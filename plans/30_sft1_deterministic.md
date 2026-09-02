# SFT1 — deterministic theorem-equivalence data at scale

> **Task ID:** SFT1
> **Status:** pilot_passed
> **Owner/session:** Claude Fable 5.1 sprint session on worktree `/localhome/milikic/LeanFaith-sft1-sprint`, branch `milikic/sft1-sprint-72h`
> **Last updated:** 2026-09-02
> **Active 72-hour sprint:** follow the compact execution path in
> [`72h_sft_data_sprint_2026-09-01.md`](72h_sft_data_sprint_2026-09-01.md). The historical
> authorization/readiness sequencing preserved below is frozen evidence, not an active dependency.
> **Active policy:** historical baseline preserved: the additive, smoke-only implementation from accepted commit
> `fc8cdc2c6d9d93e99e20933a17dbcfa2afc2be48` has produced exactly one real Mathlib preserving
> pair and one hand-written closed N31 breaking canary, serialized both, and replayed both from
> cache. This is thin plumbing/certificate evidence only: it does not compile or certify the
> frozen Wave 1 engine. All frozen revisions, receipts, hashes, and the complete 46-operation
> registry remain immutable.
> **Approval recorded:** active 72-hour sprint approved by the user on 2026-09-01; its measured
> fixture/100-root/10K gates authorize automatic progression. Older narrower approvals remain
> preserved below as historical evidence and do not constrain the additive sprint path.
> **Dependencies:** frozen REPR `176a783842c5a73b84413dfa8347670608b615d9`, current shared label
> contract, pinned Mathlib project context, gold blocklist, and the sprint's compact engine/cache/
> journal implementation. Historical census, admission, and readiness dependencies below are not
> active sprint prerequisites.
> **REPR predecessor:** `cbc933c3623d81ba649a1f9c5107ad404389d69f` was reviewed but is
> superseded and not consumable by SFT1
> **Next gate:** user decision after the stable (order-invariant, serialized-row) shortcut
> evaluation of the additive seed view `core_v2_seed` failed two of three screens:
> candidate-only 0.624 (95% upper bound 0.647, threshold 0.60) and reference-only 0.584 (0.605,
> threshold 0.60); family-held-out 0.483 (0.492, threshold 0.65) passes. The earlier `core_v2`
> pass rested on an order-sensitive minibatch screen; the deterministic full-batch screen finds a
> real leak (label-permutation control 0.52–0.55). The proposed correction is a composition
> change with additional certified negative mechanisms or N32-positive twins, not more N25
> grounding. No Lean, no regeneration, no Hub overwrite, no merge.
> **Compute class:** no active claim. The `tenk` run used one persistent Mathlib worker
> (`SFT1-SPRINT`, 1 worker / 24 GiB) across three launches with peak process-tree RSS 10.0 GB and
> released it at exit; no tmux session is running.
> Sprint outputs live under the staging root's `sprint_v1/` directory (`inventory/`, `cache/`,
> `raw/`, `runs/<run_id>/`, `compacted/<run_id>/`, `logs/`).
> **Lean budget:** the next gate may use one claimed persistent project worker with
> `Elab.async=false`; do all root selection and filtering first, issue no per-row process, cache
> deterministic results, and release the claim after live and zero-call replay checks.
> **Local staging root:** `/storage/milikic/leanfaith/value_first/sft1_deterministic_v1/`
> **HF destination:** private `Lemmy00/leanfaith-sft1-deterministic-v1`

## 72-hour sprint override (active)

This section supersedes the operational sequencing below without modifying or deleting its frozen
artifacts, receipts, hashes, or historical claims. The old admission/readiness loaders, tiered
censuses, P01 exception, 46-operation matrix, composition stack, and per-revision authorization
records remain reviewable evidence but are not dependencies of the sprint runner.

The sprint uses single-hop Mathlib transformations only:

- positive: `P15_SWAP_IFF_SIDES_V1`, `P18_SYMMETRIZE_EQUALITY_V1`,
  `P14_SWAP_INDEPENDENT_DATA_BINDERS_V1`, and `P23_CURRY_PROP_PAIR_V1`;
- negative: `N25_TOGGLE_EQ_NE_PROOF_V1`, `N32_SWAP_ROLE_ORDER_PROOF_V1`, and
  `N31_DROP_REQUIRED_GUARD_PROOF_V1`.

`P24_SWAP_INDEPENDENT_PROP_BINDERS_V1` replaces P14 only if P14 takes more than two implementation
hours or cannot yield ten accepted pilot pairs. P01, P21, composition, rubric-only negative rows,
and generic guard deletion are off. Every positive core row requires a replayed, Lean-typechecked
`Iff reference candidate` proof. Every negative requires a loaded source-theorem proof and a
replayed, Lean-typechecked `Not candidate` proof under a complete ground context; otherwise it is
terminal sidecar-only. N32 begins with strict `Nat`/`Int` `<`; `<=` is ineligible without separate
strictness or disequality evidence. N31 is restricted to bounded Nat/Int guard schemas with a
checked boundary refutation.

Implement this as a compact additive engine so the hash-bound historical `Wave1.lean` and its
Lean-free loaders remain unchanged. The runner cheaply preselects roots, reads its semantic cache
before Lean, journals one terminal per root/operation, batches work through one persistent Mathlib
worker, and deduplicates deterministically. Its cache key binds root closed-Expr hash, operation ID,
engine semantic version, Lean/project revision, and import/options context; runner and config file
hashes are provenance, not semantic cache invalidators. Core rows reject self-pairs, invalid/open
Exprs, wrong turnstile count, `[anonymous]`, `⋯`, and generated-dagger names on ordinary explicit
locals; frozen generated instance names are reported separately rather than blanket-rejected.

The 100-root run passes only if all seven operations first have one live success and one typed
rejection fixture; at least five mechanisms each retain ten pairs, including at least three
positive and two negative mechanisms; every retained proof/certificate replays; completed-terminal
resume adds zero Lean calls and no duplicate; and inspection of 30 operation-stratified pairs,
including every N31 row, finds zero wrong labels. A pass automatically launches a detached,
resumable 10,000-retained-pair run in 1,000-pair shards. The 10K output, not the 100-root run, is
the shortcut gate for larger scale: candidate-only/reference-only balanced accuracy must each be
below 0.60 and mechanism-held-out balanced accuracy below 0.65, alongside 100% proof/certificate
replay and duplicate/conflict rejection. If those checks and the measured completion projection
pass, launch larger scale in independently complete private-release shards; do not weaken negative
proof requirements to hit a row target.

## Authorization boundary

### Additive two-row thin-smoke authorization (2026-08-31)

The user's latest instruction supersedes the earlier readiness sequencing only for this bounded
experiment. From the exact accepted base `fc8cdc2c6d9d93e99e20933a17dbcfa2afc2be48`, this session
may produce exactly two local smoke rows in Mathlib: one preserving row using the simplest live
P15/P18/P21 path and one breaking row using a hand-written closed N31 guard-dropping canary. The
negative authorization is canary-specific and does not admit or activate a general N31 bank. P01,
the full census, the multi-project conformance matrix, 100 roots, 10K, production admission,
training, scale, and publication remain closed.

The thin path is exactly `source Expr -> discover applicable site -> apply transform -> replay
certificate -> render both closed Expr endpoints in one request -> serialize core row and keyed
sidecar`. It reuses the committed Wave 1 Meta engine, the central persistent Lean backend/cache,
and frozen REPR. It must claim one worker before live Lean, use `Elab.async=false`, issue no
per-row process, prove a cache replay adds zero Lean requests, and release the claim afterward.
The session stops after both rows, their proof/refutation and certificate evidence, and cache replay
are verified. If more than 2,000 new non-test lines are needed before the first pair, implementation
stops as a design failure.

The strict loader and Lean-free tests remain authorized. The corrected additive six-real-goal
direct-Expr gate has completed 6/6 and frozen the authoritative REPR-integration artifact
`v0.3.1`. That closes only the representation integration dependency; it is not an operation
admission, F1 certificate, label, or row. The successful cases prove that no forbidden residue
survived those twelve rendered endpoints. They were not live adversarial rejection probes that
injected both forbidden strings.

Revision 0.3.3 preserves and makes executable in a Lean-free effective-state loader the five
authorities that may not imply one another:

| Authority/state | Exact meaning | Current state |
| --- | --- | --- |
| user-authorized bounded implementation scope | the inherited gate approval names six operation IDs, while the current revision authorizes static task-owned implementation-readiness work for five primary mechanisms; it does not imply readiness or gate execution | active for P01, P15, P18, P21, and N31 N-RUBRIC; N31 N-PROOF is an optional evidence adapter with no required implementation binding |
| implementation readiness | global dependencies plus complete resolved dispatch/checker/anchor/bank/fixture bindings and regressions for the five primary Wave 1 IDs; an N31 N-PROOF binding is conditional on a proof-eligible project/root cell | false/pending |
| gate admission | one explicit user decision naming the wave, operations, eligible projects, and negative family/dimension admissions that may run the bounded smoke/conformance/approximately-100-root gates after readiness | true for exactly the six selected IDs and N31 `required_domain_guard`; execution remains blocked |
| production admission | a post-measurement user promotion of exact operations, versions, projects, caps, and negative dimensions from proof-of-concept or implementation-candidate status to production-eligible | false for every operation; zero production negatives |
| row emission and scale authorization | separate permission to emit model-facing pilot rows and, later, to scale or publish them | false; no 10K, bulk, publication, or count commitment |

Gate-admitted Wave 1 is exactly `P01_ALPHA_RENAME_SINGLE_V1`, `P15_SWAP_IFF_SIDES_V1`,
`P18_SYMMETRIZE_EQUALITY_V1`, `P21_BETA_REDUCE_V1`,
`N31_DROP_REQUIRED_GUARD_RUBRIC_V1`, and `N31_DROP_REQUIRED_GUARD_PROOF_V1`, each across its four
registered projects. These are six exact operation IDs but only five semantic mechanisms: the
N31 N-RUBRIC and N-PROOF IDs are two evidence lanes for the same guard-removal mutation. The five
primary operation IDs contribute 20 operation/project combinations and 40 success/rejection fixtures.
N31 N-PROOF adds one cell and two fixtures only for each project with a reproducible proof route,
so the effective matrix is dynamically 20--24 combinations and 40--48 fixtures. The independent
measurement target is approximately 500 roots across five semantic-mechanism pools; N-PROOF is an
optional evidence pass over the parent N31 pool and adds no independent roots. The five primary
current-wave IDs need resolved execution bindings; the optional proof binding is required only for
proof-eligible cells. The other 40 registry entries remain fail-closed and do not block the wave.

The former all-registry gate would cost 156 operation-project combinations, 312 success/rejection
fixtures, and approximately 4,600 roots. Those numbers are an explicit cost estimate, not a gate
requirement or row commitment. Passing the Wave 1 approximately-100-root gate permits only a
measured report and later promotion/10K decisions; it does not authorize model-facing row emission.

Revision 0.3.3 distinguishes four source-policy dimensions from execution authority. Under the
existing owner authorization for pinned `formalmathatepfl/*` inputs and the pinned Apache-2.0
license evidence for CSLib, Mathlib, and Physlib, all four sources are eligible for internal gates
and an internal pilot. That source eligibility does not complete the relevant census, admit a root,
authorize a gate, authorize 10K, or authorize publication. Redistribution review remains incomplete
and publication eligibility remains false for every source until attribution, notice, dataset-card,
release-manifest, and other applicable release checks are recorded.

The GPT Pro Wave 2 recommendation is recorded only as `proposed_not_admitted`: P14, P16, P22, P23,
P24, P28, P32-COMM, P33, P35, P40, P42, N25-RUBRIC/PROOF, N26-RUBRIC/PROOF, N30-RUBRIC, and
N32-RUBRIC. These 17 exact IDs represent 15 proposed semantic mechanisms. This record grants no
family/dimension admission, implementation, execution, production, row, 10K, scale, or publication
authority and does not modify the 46-operation registry.

### Additive revision 0.3.4 implementation-readiness layer

Revision 0.3.4 is a static, fail-closed authoring layer over the immutable revision-0.3.3
checkpoint. It prepares exactly five mandatory operation bundles: P01 alpha rename, P15 final-target
Iff-side swap, P18 final-target equality symmetry, P21 exact one-redex beta reduction, and N31
required-domain-guard removal in the N-RUBRIC lane. It does not turn the frozen all-empty 0.3.2
execution bindings into resolved bindings. The additive layer uses the distinct state
`static_authored_hash_bound`: source bytes, public symbols, operation-bank entries, anchors, fixture
specifications, and cache-key contracts may be pinned while compiled declarations, live checker
semantics, live fixture receipts, and the historical `complete_binding_hash` remain absent.
The pure 30-field cache preimage/hash helper does not bind or invoke the central persistent cache
store, and this revision does not add the persistent request adapter that will eventually dispatch,
replay, and render both endpoints in one request. Both remain explicit implementation blockers.

The task-owned Wave 1 source must not import the shared historical `TransformEngine`. That engine
has a separate pretty-printer/text-re-elaboration path, declaration-order universe naming, and an
external hashing process, all incompatible with the frozen SFT1/REPR route. The new source owns no
renderer or universe canonicalizer, creates no declaration or proof for rendering, and exposes only
typed in-session discovery, exact dispatch, and independent certificate replay over live Exprs.
Later serialization still calls the frozen REPR emitter exactly once for each explicitly unrolled
reference/candidate endpoint in one persistent Meta request.

All 20 primary operation/project cells have one static success specification and one static
adversarial-rejection specification, for 40 specifications total. They are deliberately marked
uncompiled, unexecuted, and not gate evidence. Positive applicability is captured by explicit
operation-local typed banks rather than inventing external bank identities for registry entries
whose frozen `bank_id` is null. P15 and P18 initially apply only to the final target beneath an
unchanged outer telescope, avoiding an unapproved arbitrary-context equivalence transporter. P21's
“closed argument” means globally closed at the selected redex: no mvars, universe mvars, fvars, or
loose bvars. N31 retains all five design shapes, but its proposed concrete Lean head/role entries
remain `authored_candidates_require_live_resolution`; unknown, missing, or ambiguous entries are
typed-not-applicable.

Two operation-specific blockers remain explicit:

- P01's frozen attempt-009 reference and candidate have different render hashes but the same
  alpha-invariant canonical closed-Expr hash. The frozen composition grammar rejects repeated
  closed-Expr hashes, and its narrow P01 exception does not permit an Expr-hash repeat. A new
  binder-aware exact-delta fingerprint is useful evidence but cannot override that frozen rule.
  P01 therefore remains not implementation-ready pending an additive coordinator identity/dedup
  decision.
- N31's target-head candidates cannot become an executable bank until a later authorized live Lean
  session resolves every project-scoped name, application arity, protected role position, type and
  instance constraint and replays the success/adversarial fixtures. The generic checker source may
  be authored now, but that does not close the N31 blocker. Retained contradiction patterns are a
  separate nonselectable inventory. Unknown or multiply matched retained propositions fail closed,
  and target uniqueness is checked across the selected guard shape plus every conclusion in its
  frozen implication closure, not merely within the selected target-head entry. The current source
  admits exactly zero resolved N31 bank identities, so no caller-supplied bank can dispatch N31 in
  revision 0.3.4. A future authorized revision must pin a project, bank ID, resolved-Lean hash, and
  resolution-receipt hash after verifying the exact bank hash in process. Each N31 certificate
  carries the complete typed bank and complete reachability assignment; replay rejects a different
  supplied context even when its IDs or mode strings are reused.

`N31_DROP_REQUIRED_GUARD_PROOF_V1` is not a sixth primary implementation. Revision 0.3.4 authors no
independent N-PROOF mutation or required fixture. A future proof adapter may only replay the exact
parent N-RUBRIC candidate and upgrade its sidecar after an exact source proof and exact candidate
refutation pass; absence remains `not_in_scope_for_n_proof`, never a parent or Wave 1 blocker, and
any success counts against both caps without emitting a second pair.

During an independent static-source audit, a reviewer invoked `lean --print-prefix` once while
locating the installed Lean source tree. This was outside the authorized boundary and is preserved
in the machine-readable revision-0.3.4 incident record. The command exited without loading,
importing, or compiling this project; it did not start Meta, a transformation, a gate, a census, or
row work and produced no file or artifact. No project Lean compilation or Meta validation was run,
and this incident is not evidence for any readiness or gate state.

### Additive revision 0.3.5 P01 identity-policy overlay

Revision 0.3.5 resolves only the policy contradiction described in the frozen revision-0.3.4
section above. It does not edit or reinterpret any revision-0.3.4 artifact in place. The strict
overlay first replays the complete frozen revision-0.3.4 loader closure, then removes exactly
`p01_alpha_closed_expr_hash_collision` from the composed effective blocker list.

For a chain endpoint trace, the only newly permitted canonical closed-Expr hash repetition is one
adjacent equality across the chain's sole `P01_ALPHA_RENAME_SINGLE_V1` edge. The repeated hash class
must have cardinality two; no third or nonadjacent occurrence is allowed. The immediate P01
reference and candidate must have distinct frozen-REPR render hashes and distinct exact
`sidecar.core_text()` UTF-8 bytes. The certificate-selected current typed binder site must resolve
exactly once; other eligible binder sites may exist. The exact binder-aware name-only certificate
binds the site, ordinal, chain lineage, old and new names, and `BinderInfo`. Replay must reproduce
the candidate exactly and establish that domains, bodies, bound-variable indices, universes,
metadata, other binders, and `BinderInfo` are unchanged. Alpha equivalence, definitional equality,
equal semantic hashes, or different text alone is insufficient.

At most one P01 hop may occur when P01 is present; zero remains allowed. Every retained final pair
whose chain contains P01, across either polarity and with or without other operations, counts toward
the unchanged one-pair-per-root and 0.5%-share maxima. The separately applicable 0.25%
lemma/procedure cap remains in force. All text, render, selected-site-lineage, operation,
mechanism-superclass, inverse-token, nonexception hash, duplicate, conflict, cap-order,
orientation, and cycle rules remain unchanged. Canonical unordered-pair deduplication still uses
the sorted reference/candidate render hashes, and the post-orientation global duplicate/conflict
assertion still fails a shard without commit or refill.

The Git-local attempt-009 case is only frozen evidence that equal alpha-invariant closed-Expr hashes
can coexist with distinct render hashes and model-facing texts. It is not live transform,
certificate-replay, fixture, implementation-readiness, gate, or production evidence. P01
implementation readiness and overall implementation readiness remain false behind project Lean
compilation, live success/adversarial fixtures, live certificate replay, the persistent Meta/REPR
adapter, and central cache integration. N31 resolution, the coordinator-owned shared-label
contract, and the two smoke-root micro-censuses also remain open. No execution or row authority is
created by this overlay.

The conditional review adds one successor-only implementation blocker:
`p01_identity_exception_composition_dedup_runtime_binding_and_replay`. The approved policy semantic
hash `a4aa3ddc383fdbc5fd1e161b5955f403ac17afa98f9d24defab4c2741846b4fd` remains the exact contract
the future real composition/dedup runtime must load and bind. The corrected envelope has a distinct
semantic hash and reconstructs the approved policy by removing only the correction-owned runtime
section, the one new blocker, and its false incomplete-prerequisite field; that projection must
hash back to `a4aa3ddc…`. The blocker remains `open_fail_closed` with no implementation path,
symbol, code hash, observed policy hash, binding receipt, or replay receipt. It blocks P01
implementation readiness, overall implementation readiness, and gate execution until the runtime
replays the exact acceptance, rejection, cross-polarity/composition cap, and canonical
unordered-pair duplicate/conflict contract. This correction implements no runtime.

## Objective and scale contract

Build a diverse SFT1 F1 corpus whose labels arise from exact row-local evidence plus admission of
the exact registered operation. Revision 0.3.3 makes no row-count commitment.

A 2–3M retained-pair range is a planning band to test against the zero-Lean census and later
measured pilots. It is neither a minimum, a promise, nor permission to generate rows. The arithmetic
is explicit: 500,000 eligible roots under an eight-pair cap can retain at most 4,000,000 pairs;
5,000,000 pairs would require at least 625,000 eligible roots at that cap. Any root ceiling, pair
target, 10K pilot, bulk run, stretch target, or publication requires a later explicit decision.
The older plan shorthand “5M pairs” names only that unapproved stretch scenario; it is not an
active target or frozen row commitment in policy revision 0.3.1.

Before the two-row smoke, perform only a root-specific, hash-bound, zero-Lean micro-census for each
selected smoke root. Before the approximately-100-root gate, complete the selected-wave
sampling-frame census. Before any 10K authorization request, production row-count or multi-million
feasibility decision, scale, or publication decision, complete the cross-source census over pinned
source metadata/files and
publish the full source-eligibility matrix. The complete tier reports raw theorem/lemma counts,
license/revision and release state, exact and near-duplicate clusters, source/domain/signature
strata, exact import-context availability, and the expected closed-Expr construction route. No
target may be manufactured by multiplying a root guess by a quota.

## Representation and source-to-Expr contract

The approved consumable REPR freeze is `176a783842c5a73b84413dfa8347670608b615d9`, built by
implementation commit `93cd9cf9d4848827f2bacad57a35c3d7f01500f7`. SFT1 pins all of its
independent identities:

| Binding | Frozen value |
|---|---|
| spec hash | `68d893a2c566bf3f6a82c899a32a351f9a5420f5ea98168c99b887aaa01a45a8` |
| config SHA-256 | `a65d5b29760bbc5eb89405927f946f205eb99856c0538fdf5b57d3f9eceb0db7` |
| Lean renderer SHA-256 | `4471262f812746046570c51dde5958ee33db31a450a6974071efce584ba56bc3` |
| injected-helper SHA-256 | `a6650452eebe683db295df1dfe925d3db8b03fc24e55cbc6793e838b5fe2f272` |
| Python SHA-256 | `496237e190c394e9bd3c3036e2bc01c635905116c5084787a42e6cb569f45517` |
| implementation-set hash | `9a9252fff5ffc69cb65e71120fedffa83ed47271aecadbecf0ceb890feea65ff` |
| renderer semantic hash | `0bec5429cc0e539841208be53cd52189a7b80cbdb4649ee2d45b84bd8a5ef1fd` |
| renderer API hash | `c695ad868c98f27218e82184559d90624491df25c7805bf29861dd891787261d` |
| universe profile | `goal_v1_first_occurrence_u_i_v1` / `d9e729134fcd6a086a58191810a9227062c66496ebe76b8da3c458a58b31cb61` |
| render context | `goal_v1_render_context_v1` / `5f44b6970f0902c968fc98a2659b26c1c9d0bcaef2960cd3ea73808f203f8f62` |

The earlier `cbc933c3623d81ba649a1f9c5107ad404389d69f` freeze remains recorded only as
reviewed-but-superseded evidence and is not consumable.

Reference and candidate must each be a canonical closed proposition `Expr`, alive together inside
one explicitly unrolled `run_meta do` request. The Python entrypoint is exactly
`render_closed_expr_in_session` on route `closed_expr_in_session`. SFT1 calls
`LeanFaith.GoalV1.emitClosedProp` exactly once for each endpoint; the frozen emitter owns the shared
renderer invocation and payload construction. Complete `ClosedExprSidecar` objects are persisted,
and only `sidecar.core_text()` may enter model-facing fields.

SFT1 may not copy the renderer or its options, surface-render a candidate, pretty-print and
re-elaborate a candidate, declare candidate theorem/axiom endpoints, synthesize candidate proofs,
use `sorry`, or compile/re-elaborate `goal_v1` text. A fixed SFT1-owned helper may elaborate source
proposition syntax before the runtime action only after its exact bytes and compile context are
hash-bound and reviewed; it is not a candidate or rendered-text round trip.

The frozen API rejects expression/universe metavariables, free variables, loose bound variables,
ill-typed expressions, non-`Prop` expressions, and anonymous binders that would become unsupported
rendered locals. It deliberately preserves nondependent explicit structural arrows. SFT1 also
rejects final output containing `[anonymous]` or `⋯`, and requires exactly one turnstile. Both Exprs
use the frozen universe profile above; task-local universe naming may not silently diverge.

SFT1 has independently passed the corrected additive six-real-goal direct-Expr gate. Authoritative
attempt `attempt_009` retained all 6/6 pairs and 12/12 rendered endpoints in 21.546 measured
Lean-seconds, persisting 119,895 sidecar bytes. Every required-distinct render differed, every output
had exactly one turnstile, both complete sidecars persisted, and no forbidden `[anonymous]` or `⋯`
residue survived. The frozen regression is
`sft1_repr_six_real_goal_direct_expr_v0_3_1`; its receipt is
[`repr_six_goal_gate_receipt_v0_3_1.json`](../configs/transformations/sft1_value_first_v1/repr_six_goal_gate_receipt_v0_3_1.json),
with checked-in file SHA-256
`ebd400b4a7b05daa933b1abaaacc378d1a7b9ae68f9159ac03453cd6081406a8` and semantic receipt hash
`f62b68ebc946469952bdd34674c127e2bd1146b0a8febbe5d199fea54a081e78`.

Attempt 009 is independently reviewable from Git through the small repo-relative
[`repr_six_goal_evidence_v0_3_1/`](../configs/transformations/sft1_value_first_v1/repr_six_goal_evidence_v0_3_1/)
bundle. Its manifest binds all six case files, their canonical evidence/request hashes, the receipt,
execution preimage, helper/preamble, timings, byte total, exact residue-class mappings, and the
limited clean-success claim. The manifest raw file SHA-256 is
`aeb44673d45ce3bb31923fec7ab402c40aefdefa108302deed0b9f0fe0246d46`; each of the six
checked-in case files has its own raw hash, and canonical minified JSON reproduces the original
attempt-009 receipt evidence hashes. The original `/storage/milikic/.../attempt_009` directory
remains supplemental provenance; it is not the only receipt-replay path and is not required for Git
review.

The exact execution preimage is
[`repr_six_goal_gate_execution_v0_3_1.yaml`](../configs/transformations/sft1_value_first_v1/repr_six_goal_gate_execution_v0_3_1.yaml),
with raw file SHA-256 `82f22c08082e26424e1a55627b707d341e7fa84f72348cfed0b007b0526505ff`
and effective canonical hash
`dfc7037ee8d5a340b82b237fa14ef1f3d9c2752bf64e91d34846d9570fac5747`. The post-pass binding is
[`repr_six_goal_gate_v0_3_1.yaml`](../configs/transformations/sft1_value_first_v1/repr_six_goal_gate_v0_3_1.yaml),
with raw file SHA-256 `5126eb8fb314218017fc930a79ab82cb810ff929e1794ce4617551f6c70ced91`
and effective canonical hash
`7404e31935ab35b9c3270bf46654936121944a7e8f55fb91da4f1e047f59c0ad`. The reviewed helper file
and injected-preamble hashes remain `c87b9c5065a41f51e7cbdcdcc98f14fedc6a015054c40a0cfe4367ad63330129`
and `bd0e3ef6b5e5c50bf07b31771e2a2ca0da131323d10d8571994bdd24a922981a`.

The v0.3.1 correction binds the canonical-gold source file and extracted formal statement at
`a0c4d102a0ea4d2923cca85129c6cda054a11b1854462eed3d7e71e555b703ea` and
`8b0061199a23b47539e6f30df775109d5c6776ea1c2206f452d5a9d48240aa7e`, respectively, and narrows
each compile context to source-faithful imports and notation. Only `mathlib_add_pow` and the
ConsistencyCheck case open `BigOperators`; canonical gold uses `import Mathlib` with no notation
scope, the compiler case uses `import Init.Sym.Lemmas`, and Physlib/CSLib use only their exact
imports. `cslib_ret_merge` keeps the `TimeM` scope closed and renders the fully qualified type.

The previously passing `attempt_008` v0.3.0 config and receipt remain preserved as superseded
evidence. They are not the active SFT1 dependency because v0.3.1 adds the exact execution-config
preimage, canonical-gold source pins, and minimal compile-context bindings. Attempts before 008
failed closed and produced no success receipt; they exposed a misnamed `TermElabM.run'` helper route,
an unqualified Lean compiler constant, canonical-gold binder/delimiter failures, and legacy
ConsistencyCheck big-operator syntax. The repairs remain confined to the reviewed helper and exact
project contexts. These are representation/context discoveries only: every P01-like alpha candidate
has `production_admission: false` and is neither a registered-transform implementation nor a
training row.

The two source routes are separate:

- **Imported constants (Mathlib, Physlib, CSLib):** look up `ConstantInfo`, take its complete type,
  instantiate its universe parameters with the canonical recorded universe profile, then require
  closure, successful type inference, and `Prop`. The resulting type `Expr` is the reference.
- **`compiler_data` and signature-only sources:** the zero-Lean census first verifies pinned
  signature text and exact compile context. The reviewed, hash-bound task preamble elaborates the
  complete binder telescope and result directly into a proposition `Expr`, instantiates
  metavariables once, and rejects unresolved/open/non-`Prop` results. The runtime action consumes
  that live Expr; it never inserts a theorem declaration or parses candidate/rendered text. A
  signature lacking a reproducible context is source-ineligible.

Both routes feed the same Expr renderer and record builder, context, toolchain, imports, options,
universe-profile ID/hash, and renderer/spec hashes.

### P23 binder-hygiene correction

The shared frozen `LeanFaith/Meta/TransformEngine.lean` is not consumable for P23: its packing and
unpacking construct `Name.anonymous` Pi binders, and a live probe rendered `[anonymous] : True✝`,
which the frozen representation validator correctly rejected. Do not edit that shared path.

The registered P23 operation is pack-only. Its future approved SFT1 implementation belongs in
`LeanFaith/Meta/SFT1/TransformEngine.lean` and must construct its one generated conjunction-proof
Pi name with `Name.mkSimple`: choose `h` if capture-free, otherwise `h_<n>` for the smallest positive
ASCII decimal `n` without a leading zero that is absent under both Lean `Name` equality and the
replacement renderer's displayed/sanitized local-name equality across the complete original
telescope. Naming may depend only on the canonical closed Expr and selected site, never family,
operation, label, root/row ID, seed, or randomness. The choice must be stable for the same Expr/site,
preserve every surviving binder name/lineage, and may never use `Name.anonymous`.

Before P23 can pass the one-example gate, a regression must inspect the candidate Expr binder names
and the final shared-API rendering, proving that no anonymous binder, `[anonymous]`, or `⋯` reaches
`goal_v1`. Nondependent explicit structural arrows remain valid and must not be rewritten into
artificial named locals merely to satisfy this check.

## Label contract

### Positive operations

A positive row needs exact `P-DEF`, `P-SCHEMA`, `P-LEMMA`, or `P-REFLECT` evidence for the
emitted closed pair plus production admission of its exact operation ID and separate row-emission
authorization. A gate-admitted operation may produce only bounded non-training gate artifacts.
Family polarity or endpoint provability cannot create a label.

Every `P-LEMMA` and `P-REFLECT` operation must carry and adversarially test all four
claim-erasure guards:

1. reject reuse of a lemma that proves or rewrites the complete claim;
2. reject reflexive or `True`/`False` collapse;
3. reject deletion of a hypothesis by rewriting it to `True`;
4. reject reflective normalization of the root relation/whole claim.

### Negative lanes

The negative contract has two explicit lanes:

- **N-RUBRIC:** one exact typed mutation of a protected claim dimension already named by the shared
  consistency rubric. Each exact operation binds typed applicability, context restrictions,
  operation-specific anti-degeneracy checks, exact-delta reconstruction evidence, adversarial
  fixtures, and stage-specific user admission. This lane says that the candidate changes the
  admitted protected claim dimension; it makes no claim about F2 direction or candidate truth.
- **N-PROOF:** a stronger, capped subtype of an admitted N-RUBRIC operation. It retains and replays
  an exact source proof and an exact candidate refutation for the same closed Expr pair under an
  allowed axiom profile. Aggregate N-PROOF share is capped at 10%, with stricter per-operation caps.

Generic `D0`, failed proof search, F2 direction, and abstract separators are non-label evidence.
Every retained sidecar records candidate truth evidence as exactly `proved`, `refuted`, or
`unknown`; that field is separate from the F1 label and cannot select a lane or create a label.

N-PROOF is optional per source, project, and root. Its eligibility is exactly parent N-RUBRIC
applicability plus an available exact source proof plus replayed exact candidate refutation.
Missing or unknown proof evidence yields `not_in_scope_for_n_proof`; it is not a parent-operation,
wave, or rubric failure. The proof pass uses the already-selected parent mutation root pool and may
not create a separate sampling stratum, diversity quota, or core pair. It is a sidecar evidence
upgrade of that pair. Every upgraded pair counts against the N-PROOF cap and against the parent
semantic-mutation cap, whose denominator is the natural model-facing retained semantic-pair
population after duplicate/conflict screening and before training-orientation swapping. The parent
cap counts unique parent pair IDs including the proof-upgraded subset; parent and proof counts are
not added together when evaluating the parent cap. Synthetic-track denominators remain separate.

### Negative-operation promotion contract

The current freeze permits **zero production negatives**. An N-RUBRIC or N-PROOF operation moves
from proof-of-concept to production-eligible only through all of these distinct, recorded states:

1. **Wave selection and gate admission:** the user names the exact operation ID/version, eligible
   projects, protected family/rubric dimension, lane, cap, and wave. This permits only bounded gate
   work after implementation readiness; it is not production admission.
2. **End-to-end smoke and conformance:** the wave first produces one actual serialized positive and
   one actual serialized negative example, each with its complete sidecar, manifest link, cache
   replay, and duplicate-suppression replay. It then passes one success and one expected
   adversarial rejection for every selected operation-project combination. A certified N-PROOF
   drop must replay both exact source-proof and candidate-refutation fields and record truth as
   `refuted`; an uncertified N-PROOF drop must carry neither proof field. Partial fields are invalid.
3. **Measured approximately-100-root gate:** the selected negative operation is evaluated over
   approximately 100 eligible roots with 100% retained-certificate replay and reports applicability,
   anti-degeneracy and exact-delta results, candidate-truth distribution, terminal failure classes,
   cache/replay behavior, duplicate/conflict outcomes, source/polarity yield, Lean-seconds, sidecar
   bytes, RSS, and shortcut/surface-residue plus held-out balanced-accuracy diagnostics with
   confidence bounds. This report—not a single fixture—is the measured evidence that can support
   promotion.
4. **Explicit production-promotion decision:** after reviewing that report, the user records a new
   decision naming the exact negative operation/version, family/dimension admission, lane,
   projects, hashes, axiom profile, and cap to promote to `production_eligible`. A sibling lane is
   not promoted implicitly. Production eligibility still does not authorize row emission, a 10K
   pilot, scale, or publication.

In the machine state, “production-eligible” is the conjunction of transition to
`implementation_candidate` and an exact production-admission record; neither half is sufficient.
N-PROOF additionally requires production admission of its parent N-RUBRIC operation, and its cap
may not exceed the parent's cap.

The production-promotion record must therefore say, in substance: “Promote exactly `<operation
IDs and frozen versions>` under `<measured selected-wave receipt>` to production-eligible for the
named projects, family/rubric dimensions, lanes, axiom profiles, and caps; do not authorize row
emission, a 10K pilot, scale, publication, or any row-count commitment.” Until such a record exists,
successful proof-of-concept fixtures and gates remain evaluation evidence only.

All evidence closes the complete telescope and binds universes, ordered locals, binder kinds, local
definitions, implicit/instance binders, coercions, hypotheses, target, transparency, logic regime,
dependencies, and allowed axiom profile.

The adversarial review added a pre-implementation hardening gate: separately typed receipt results
for reference validity, candidate validity, F0/defeq, F1 certificate, candidate truth, optional F2
direction, and final retain/drop; exact per-operation dispatch/checker bindings; global
binder/domain and empty-domain profiles; environment/normalization fingerprints; correlation and
effective-diversity groups; distinct bounded-gate versus production eligibility; exact gate
counters; and an early 100-root certificate/provenance-residue surface screen. N26, N31, and N32
use closed design guard/relation banks rather than open semantic search: N26 is restricted to
boundary-bearing contexts whose claim relevance is mechanically checked (not generic exponent,
index, or upper-bound edits); N31 admits only named guard schemas such as nonzero, positivity,
nonnegativity, membership, and index bounds; and N32 admits only exact role-sensitive `Nat`/`Int`
`LT`/`LE` heads, excluding `Eq`, `Iff`, arbitrary relations, and failed symmetry search. These
requirements are now encoded and invariant-tested in the strict machine policy. Their concrete
checker, dispatch, fixture, resolved-anchor, and applicability-bank implementations remain
fail-closed and must be pinned before either operation enters a selected wave's conformance gate.

Dropped receipts are not exempt from evidence discipline. Their exact terminal reason must select
the coherent allowed states of reference validity, candidate validity, F0/defeq, F1 certificate,
candidate truth, optional F2, and disposition. Evidence-class/F1 direction invariants continue to
hold on every drop, and an N-PROOF receipt always obeys the source-proof/candidate-refutation field
contract: certified receipts require both hashes and `refuted` truth, while uncertified receipts
forbid both hashes. A contradictory drop receipt is a schema error, not a counted transform
rejection.

Concretely, the root-level blocklist rejects a root before any candidate receipt exists.
`candidate_closed_prop_invalid` requires a valid reference, failed candidate, unknown F0/truth, no
F2, and uncertified F1. `no_op_dropped` and `cancellation_dropped` require valid endpoints,
definitionally equal F0, and uncertified F1. The post-transform `blocklist_dropped`,
`f1_relation_uncertified`, `vacuity_rejected`, and `empty_domain_rejected` paths require a valid
candidate and uncertified F1. Duplicate and split-cluster terminal drops occur only after a
class-consistent preserving/breaking F1 certificate. Retain requires the same completed certificate.

The review does not change F1 into F2: N-RUBRIC need not prove `¬(A ↔ B)` or candidate falsity, and
a whole-endpoint `Iff` proof is not by itself positive F1 evidence. Exact transform-local claim
evidence remains the label basis.

## Exact operation registry and frozen banks

The authoritative registry is
[`proposed_composition_policy.yaml`](../configs/transformations/sft1_value_first_v1/proposed_composition_policy.yaml).
Family prose is descriptive and cannot authorize an umbrella family.

Every one of its 46 exact operations records: operation ID; family, track, status, and lane;
mechanism superclass; schema/lemma/procedure anchor hash; orientation; typed applicability; context
restrictions; transparency; logic regime; allowed axiom profile; inverse token; exact cap; heartbeat,
soft, and hard time budgets; eligible projects; success and adversarial-rejection fixtures;
claim-erasure guards where applicable; candidate-truth default; exact-delta requirement; and an
operation-local admission object. Gate and production admission are separate. The frozen base
registry retains its historical six `pending_gate_admission` entries and 40 `not_selected` entries;
revision 0.3.3's typed effective-state loader composes that immutable snapshot with the exact Wave 1
admission receipt instead of rewriting the registry. The resulting effective state gate-admits only
the six Wave 1 IDs. Production admission, executability, and label/row emission remain false for all
46.

Nine exact family-and-rubric-dimension records bind every N-RUBRIC operation and its N-PROOF
sibling, including the two separate N28 synthetic dimensions. The frozen base record preserves the
N31 required-domain-guard proposal; the additive receipt records its exact Wave 1 gate admission.
The other eight remain `not_selected`, and all nine production admissions remain false. Neither an
operation gate admission nor a family disposition can substitute for the matching family/dimension
gate and later production decisions.

The design-frozen
[`starter_banks_v0_3_0.yaml`](../configs/transformations/sft1_value_first_v1/starter_banks_v0_3_0.yaml)
contains hash-bound starter entries for P20, P32, P34, P35, P39, P41, P42, the N-RUBRIC protected
dimensions, N-PROOF templates, and separate N28 synthetic templates. Every entry is either bound to
exact operation IDs or explicitly `reserved_unadmitted`. Its Lean-resolved anchor hashes remain
null, which is intentional: only the anchors/banks used by operations selected into a current wave
must be resolved and pinned before that wave can become implementation-ready. Unselected entries
remain unresolved and fail-closed without creating an all-46 barrier.

The exact registry dispositions are:

- P02 and P11 are diagnostic and cannot emit rows.
- The frozen P01 registry has one narrow exception allowing its single hop to repeat an alpha
  fingerprint once and still forbids text, closed-Expr, render, operation, and inverse-token
  repeats. Revision 0.3.5 does not modify that object; its additive effective overlay separately
  permits only the certificate-qualified adjacent closed-Expr equality defined above. Text,
  render, operation, and inverse-token repeats remain forbidden.
- P21 beta/zeta introductions are diagnostic at a 0.1% cap; reductions remain implementation
  candidates.
- P39, P41, P42, and both lanes of N29–N32 remain proof-of-concept; N31 has the highest negative
  priority.
- N21 and N22 are redesign-only and have no operation entries.
- N28 has four proof-of-concept operations in a separate synthetic track that cannot mix with
  natural roots or share cap denominators.
- Unregistered operations, bank entries marked `reserved_unadmitted`, diagnostic operations, and
  proof-of-concept operations that have not passed their exact gate cannot emit labels.

## Existing and planned family audit

The complete preserving/breaking audit is
[`PROPOSED_TRANSFORM_AUDIT.md`](../configs/transformations/sft1_value_first_v1/PROPOSED_TRANSFORM_AUDIT.md).
It inventories all current and planned families, distinguishes historical mechanics from label
evidence, and reports applicability, composition safety, expected value, Lean cost, disposition,
and caps. Frozen v1/v2 artifacts remain evidence only.

Old D0 negative families (`N01`, `N02`, `N03`, `N07`, `N10`–`N18`) remain candidate or
diagnostic evidence, not binary labels. No historical row is relabeled by this proposal.

## Composition grammar and cancellation safety

Only these productions are valid:

```text
positive_row := P | P P | P P P
negative_row := N | P N | P P N
```

For a model-facing row, `P` is a production-admitted positive exact operation and `N` is one
production-admitted N-RUBRIC operation or its separately admitted capped N-PROOF subtype; row
emission must also be authorized. For bounded gate artifacts, the same grammar applies to exact
gate-admitted operations without granting production or row-emission status. A positive row has
zero negative operations; a negative row has exactly one, last and unique. Each hop rediscovers a
unique typed site in the current Expr. Sites are pairwise
disjoint, mechanism superclasses do not repeat, inverse tokens do not repeat, and
text/render/site-lineage cycles reject the chain. Closed-Expr cycles also reject except for the
single certificate-qualified adjacent equality across the sole P01 hop in revision 0.3.5; the
frozen one-hop alpha-fingerprint exception remains unchanged. No other repetition or cycle escape
is permitted.

The seven exact P20 fold/unfold, P21 beta/zeta introduction/reduction, and P22 eta-reduction
operations form one explicit mutual-exclusion group: at most one of them may appear in a chain.
This makes the single-definitional-mechanism rule executable rather than relying on family prose.

The deterministic retention order is: source eligibility; typed applicability; root blocklist;
pre-validation candidate sampling; typed candidate Meta construction/closure/type/render
validation; post-transform blocklist; typed F1 evidence validation/replay; stable row-hash total
ordering; canonical-unordered-pair duplicate/conflict classification; retain
the minimum stable row hash for same-label duplicates and reject the entire class for conflicting
labels; per-root, operation, bank/template, lemma/procedure, family, mechanism, source, and joint
source×polarity caps; deterministic training orientation swap; then a final model-facing
duplicate/conflict assertion. That assertion must pass or the shard is not committed or refilled.
Caps are maxima, not quotas, and natural/synthetic denominators are separate.

The stable row hash uses canonical source/root identity, both closed-Expr hashes, operation-chain
and selected-site lineage hashes, label, evidence/certificate payload hash, and the renderer,
REPR-spec, universe-profile, and render-context hashes. The unordered-pair class key is the hash of
the two rendered-output hashes in sorted order, so provenance never decides duplicate survival by
filesystem or worker order.

The root and post-transform evaluation screens bind the exact hash of
`data/benchmarks/golden_blocklist_v1.json`
(`8e4af6a9e47fb06d281169cdaddb01c5c66c1b0d150f2df9c9283ecb587117f7`); a match is excluded and
counted before retention.

## Lean is the bottleneck: execution contract once readiness prerequisites pass

Lean remains the bottleneck:

The earlier shorthand “no per-pair Lean compilation” is superseded by the stricter operational
rule below: no row spawns a process, while every retained row is validated and replayed inside a
bounded persistent Meta worker.

- cheap source filtering, census, joins, deduplication, candidate discovery, and deterministic
  pre-validation sampling happen first;
- pre-validation sampling is seedless: sort by the hash of source closed Expr, operation ID,
  selected-site lineage, and candidate closed Expr, then take the operation-budget prefix;
- no row starts a process; a bounded number of persistent project/toolchain Meta workers validate
  batches;
- every retained row receives typed closure/type/`Prop` validation and 100% evidence/certificate
  replay in persistent Meta;
- source, candidate, operation-entry, bank-anchor, evidence, and renderer hashes are computed in
  process;
- every operation has a heartbeat interval and soft/hard budget; deterministic failures are not
  retried;
- each root has a separate append-only durable journal, stable IDs, atomic completion, deterministic
  resume, and duplicate suppression;
- measurements include startup time, cache hit rate, failure class, memory, Lean-seconds per
  retained pair, and sidecar bytes per retained pair;
- cache keys bind both Expr hashes/builders, universe profile, Lean/project/toolchain/imports/options,
  synthesized instances, operation and registry-entry hashes, evidence/certificate payload hash,
  anchor and resolved-bank hashes, transparency, axiom profile, validator/evidence versions,
  replacement commit, renderer API hash, REPR spec hash, canonical-universe-profile hash,
  render-context ID/hash, and policy hash.

This replaces the old “sampled Lean” formulation. Sampling reduces candidates before validation;
it never exempts a retained row from typed validation or certificate replay.

## Representation pre-gate acceptance

Before the Wave 1 end-to-end smoke can start, all of the following must be true:

- the approved REPR freeze and every API/source/spec/universe/render-context identity above remain
  byte-for-byte pinned;
- the passed SFT1 six-goal receipt and its fixed reference-elaboration helper, project-specific
  namespace/notation/import/options contexts, and all complete sidecars remain hash-bound;
- the additive shared-contract rule for exact evidence plus family/dimension and operation admission
  has been merged and pinned;
- the strict machine policy's already-enforced reference-validity, candidate-validity, F0/defeq,
  F1-certificate, candidate-truth, optional-F2, and final-disposition axes are populated with one
  resolved dispatch target, certificate checker, side-condition profile, anchor/bank, and
  adversarial fixture bundle for each operation selected into Wave 1; unselected operations may
  remain unresolved and fail-closed;
- reference and candidate closed Exprs render successfully through `render_closed_expr_in_session`
  in the same persistent `run_meta do` request, with one explicitly unrolled
  `LeanFaith.GoalV1.emitClosedProp` call per endpoint;
- a pair required to be distinct has distinct output, and each rendering contains exactly one
  turnstile;
- neither Expr nor rendering contains expression/universe metavariables, free variables, loose
  bvars, unsupported anonymous rendered locals, `[anonymous]`, or `⋯`, while nondependent explicit
  structural arrows remain supported and type inference succeeds before either render;
- P23's no-anonymous-binder regression must pass before any future wave selecting P23; P23 is not
  selected into Wave 1 and therefore does not block that wave;
- all six exact SFT1 real-goal direct-Expr cases continue to replay against authoritative receipt
  `f62b68ebc946469952bdd34674c127e2bd1146b0a8febbe5d199fea54a081e78`;
- failures are counted by source, family, operation, polarity, and exact failure class; and
- stable IDs and sidecars bind reference/candidate closed-Expr and rendered-output hashes, the
  implementation/freeze commits, renderer/spec/config/helper/API hashes, implementation-set and
  semantic hashes, canonical-universe-profile ID/hash, render-context ID/hash, fixed-preamble hash,
  and project compile-context hash.

The exact failure taxonomy includes reference/candidate render failure, Expr or universe mvar,
free variable, loose bvar, unsupported anonymous binder, `anonymous_binder_name`,
`forbidden_rendered_placeholder`, ill-typed/non-Prop Expr, wrong turnstile count,
required-distinct collapse, universe-profile mismatch, render-context mismatch, and missing REPR
coverage evidence.

The mapping is exact: rendered `[anonymous]` maps to `anonymous_binder_name`, while rendered `⋯`
maps to `forbidden_rendered_placeholder`. Lean-free behavioral tests inject each literal and require
its own class. The six successful real-goal cases established only that neither residue survived
their outputs; they were not live adversarial rejection probes for those literals.

The representation-specific items above are satisfied by the passed 6/6 receipt. They do not
satisfy the shared-label-contract, Wave 1 gate admission, or selected-wave
machine-schema/dispatch/certificate/fixture requirements, so transform execution readiness remains
closed despite the user's bounded implementation-scope authorization.

## Gates

### Six-real-goal REPR integration gate

Completed and frozen: `mathlib_add_pow`, `physlib_kinetic_energy_conserved`, `cslib_ret_merge`,
`lean_compiler_int_lt`, `canonical_gold_aime_1983_p1`, and
`consistency_check_amc12a_2019_p21` all passed through the direct closed-Expr route in authoritative
attempt 009. Each project-local request kept the canonical reference and distinct type-correct alpha
candidate alive together, emitted exactly two endpoints, and persisted both complete sidecars. The
v0.3.1 receipt binds every request/evidence hash, 21.546 aggregate measured Lean-seconds, and
119,895 sidecar bytes. The canonical-gold file/formal hashes and minimal source-faithful compile
contexts are explicit; the `TimeM` scope remained closed for CSLib and validation was never
weakened. Attempt 008 remains preserved as superseded v0.3.0 evidence, not the active receipt.

### Zero-Lean census

Use three separate zero-Lean tiers. Before the two-row smoke, each exact root receives a
root-specific, hash-bound micro-census covering pinned source/revision, source-policy eligibility,
root identity, reproducible project/toolchain/import/options context, closed-Expr route, root
blocklist, exact duplicates in the smoke pool, typed operation applicability, and the minimum stable
eligible-root hash in that pool. Before the approximately-100-root gate, complete a selected-wave
sampling-frame census with candidate pools, deterministic-prefix sufficiency, exact/near-duplicate
clusters, source/domain/signature strata, proof-route coverage, and applicability denominators. The
complete cross-source census remains mandatory before any 10K request, production-count,
multi-million feasibility claim, scale, or publication decision, but it does not block the smoke,
selected-wave conformance, or approximately-100-root measurement.

### One-positive/one-negative end-to-end smoke

After the already-recorded Wave 1 gate admission and all implementation-readiness checks pass,
serialize one actual positive and one actual negative example end to end. Each must include the
final core-row projection, complete sidecar, content-hash manifest link, stable ancestry and
operation-chain IDs, durable journal record, measured Lean/cache accounting, successful replay
from the exact cache key, and a second-attempt duplicate-suppression result proving that no duplicate
row is appended. Replay 100% of both certificates in the same persistent Meta architecture intended
for the next gate. These are bounded gate artifacts, not authorization to emit a training split.

The smoke bindings are exact: the positive uses `P01_ALPHA_RENAME_SINGLE_V1`; the negative uses
`N31_DROP_REQUIRED_GUARD_RUBRIC_V1`. After each operation's root-specific micro-census is complete,
its root is selected seedlessly as the minimum stable eligible-root hash in that hash-bound smoke
pool. Counts alone or a convenient hand-picked fixture cannot substitute for these bindings.

The REPR/six-goal dependency is satisfied, but this smoke must not start until the additive shared
label contract is merged and pinned, both root-specific micro-censuses pass, the five primary Wave 1
bindings and fixtures resolve, and the closed N31 rubric checker is hash-bound. No N31 source-proof
route is required for the rubric smoke; proof availability cannot block it.

### Selected-wave operation conformance matrix

Only after the two-example end-to-end smoke passes, require one successful candidate and one live
adversarial rejection with the expected terminal reason for every primary operation-project cell.
Wave 1 has five primary IDs across four registered projects: 20 base combinations and 40 fixtures.
N31 N-PROOF contributes a conformance cell only in each proof-eligible project, so the exact matrix
is 20--24 combinations and 40--48 fixtures. Missing proof coverage is reported as coverage and is
not a failed fixture. Every dropped receipt remains subject to coherent terminal-axis validation,
evidence-class/F1 direction, and N-PROOF proof-field discipline. Zero primary-operation yield is not
silently waived; a census-backed incompatibility requires a policy revision or removal from the
wave. Unselected operations remain fail-closed and do not participate.

For comparison only, the former all-46 matrix would be 156 operation-project combinations and 312
fixtures. It is not a prerequisite for Wave 1.

### Approximately 100 eligible roots per selected-wave semantic mechanism

Only after the selected-wave conformance matrix and selected-wave sampling-frame census pass,
evaluate approximately 100 eligible roots per semantic mechanism, again with 100% retained-row
typed validation and replay. Wave 1 therefore plans approximately 500 independent roots across P01,
P15, P18, P21 beta reduction, and N31 guard removal. N31 N-PROOF is attempted only as an optional
evidence pass on that same N31 parent pool wherever exact proof evidence exists; it creates no sixth
root pool. The all-46 design would have required approximately 4,600 roots. Report applicability,
pre-validation sampling, successes/rejections, terminal and representation failure classes, cache
behavior, Lean-seconds, sidecar bytes, RSS, source/polarity yield, proof-route coverage,
candidate-truth distribution, shortcut/residue diagnostics, and deterministic resume/replay.

### Later 10K decision

After the selected-wave approximately-100-root report, stop. First record any exact
operation-production promotions supported by the measurements, especially the separate N-RUBRIC
and N-PROOF promotion decisions described above. Only then request a separate user decision for a
measured 10K pilot. A future 10K pilot must satisfy all of the following, with 95%
stratified-cluster-bootstrap confidence bounds whose upper bounds remain below the thresholds:

- candidate-only and reference-only balanced accuracy each strictly below 0.60;
- paired family-, mechanism-, and template-held-out balanced accuracy each strictly below 0.65;
- deterministic 50% orientation swap in training only, after caps and the orientation-invariant
  duplicate/conflict screen but before the final model-facing assertion;
- intact root-ancestry and near-duplicate clusters across splits;
- root-level and post-transform evaluation-blocklist screens;
- global model-facing duplicate and conflicting-label rejection;
- joint source×polarity stratification without forced rows; and
- 100% retained-certificate replay in persistent Meta.

The former `0.70` shortcut allowance is superseded: policy revision 0.3.1 uses only the stricter
candidate/reference and paired held-out balanced-accuracy thresholds above, each with its required
confidence bound.

The 10K pilot, any bulk scale, publication, and all row/root-count commitments are unauthorized.

## Acceptance criteria for this revision

- Commit `505b74754f881e903b5f04eab99311a125484b24`, revision 0.3.4, every earlier
  revision, every pre-existing receipt, and the exact 46-operation registry remain preserved. The
  current change is one Lean-free corrective revision over the approved 0.3.5 policy.
- The strict Lean-free loader composes and replays the immutable revision-0.3.4 closure; binds the
  exact parent commit/tree, P01 operation/bank/bundle, REPR identities, collision evidence, and
  unchanged grammar/dedup/cap contracts; rejects unknown, duplicate, contradictory, alternate-path,
  or hash-drifting state; and exposes no execution, transformation, or row-emission surface.
- The only permitted closed-Expr repetition is the qualified adjacent P01 equality above. Only its
  original policy blocker is removed. The replacement composition/dedup runtime-binding blocker is
  present exactly once and remains open. P01 implementation readiness, overall readiness, and every
  gate, execution, row, production, Wave 2, 10K, scale, training, and publication state remain false.
- The future runtime must bind `a4aa3ddc…`, accept only the adjacent two-endpoint repeat across the
  sole P01 edge after exact certificate replay, reject every named adversarial case, count every
  positive or negative direct/composed P01 chain against its caps, and preserve canonical
  unordered-pair duplicate/conflict handling. This revision records those requirements but does not
  implement or replay them.
- Effective Wave 1 contains six exact IDs but five semantic mechanisms. Its conformance accounting
  is 20--24 project cells and 40--48 fixtures; its independent measurement target is approximately
  500 roots. Optional N31 N-PROOF cells and evidence are derived only from reproducible proof routes
  over the parent N31 root pool.
- The root-specific micro-census precedes the two-row smoke, the selected-wave sampling-frame census
  precedes the approximately-100-root gate, and the complete cross-source census precedes any 10K,
  production-count or multi-million feasibility claim, scale, or publication decision.
- Internal-gate, pilot, redistribution-review, and publication source eligibility are separate from
  each other and from gate or scale authorization. Existing owner/Apache evidence supports the
  first two; redistribution review and publication eligibility remain false.
- N31 N-PROOF is optional per source/project/root, cannot block N31 N-RUBRIC, and counts as a nested
  evidence upgrade under both its own cap and the parent semantic-mutation cap without additive
  core-pair volume.
- The exact 17-ID/15-mechanism GPT Pro Wave 2 list is recorded only as
  `proposed_not_admitted`; every implementation, admission, execution, and emission flag is false.
- The reviewed `cbc933…` predecessor is explicitly non-consumable; the approved `176a783…` freeze
  and all supplied identities are exact typed-loader dependencies. SFT1 coverage is frozen as
  `sft1_repr_six_real_goal_direct_expr_v0_3_1` / `f62b68e…`, passed 6/6 and 12/12 endpoints.
- Negative labels use N-RUBRIC or its capped N-PROOF subtype; D0 and candidate provability do not
  create labels, and the current freeze permits zero production negatives.
- Every dropped receipt obeys terminal-reason/axis coherence, evidence-class/F1 direction, and
  N-PROOF proof-field invariants; `[anonymous]` and `⋯` map respectively to
  `anonymous_binder_name` and `forbidden_rendered_placeholder`.
- Every P-LEMMA/P-REFLECT entry carries all claim-erasure guards.
- The tiered zero-Lean census, source routes, exact grammar, axiom profiles, cache keys,
  deterministic cap order, source/polarity balancing, cap denominator, scale arithmetic, and gates
  are invariant-tested.
- The six-goal run used bounded persistent project environments and produced only representation
  evidence. This revision runs no Lean, transform, census-scale job, gate, or generation and grants
  no production, 10K, scale, publication, or frozen/shared-artifact mutation authority.

## Writable paths and ownership

**Writable SFT1 areas after the applicable gate:** this brief; `src/leanfaith/sft1/`;
`LeanFaith/Meta/SFT1/`; `configs/transformations/sft1_value_first_v1/`;
`tests/unit/sft1/`; named SFT1 live fixtures; and the SFT1 staging root.

Shared plans, REPR, existing transform/Meta engines, project/dependency config, historical outputs,
frozen manifests, and user work are read-only. Changes there require a coordinator request.

**Cumulative SFT1 task-owned and historical paths:** the list below records prior SFT1 ownership;
it does not grant this revision permission to rewrite frozen evidence.

- `plans/30_sft1_deterministic.md`
- `configs/transformations/sft1_value_first_v1/PROPOSED_TRANSFORM_AUDIT.md`
- `configs/transformations/sft1_value_first_v1/proposed_composition_policy.yaml`
- `configs/transformations/sft1_value_first_v1/starter_banks_v0_3_0.yaml`
- `src/leanfaith/sft1/__init__.py`
- `src/leanfaith/sft1/composition_policy.py`
- `src/leanfaith/sft1/repr_six_goal_gate.py`
- `LeanFaith/Meta/SFT1/RepresentationGate.lean`
- `configs/transformations/sft1_value_first_v1/repr_six_goal_gate_v0_3_0.yaml`
- `configs/transformations/sft1_value_first_v1/repr_six_goal_gate_receipt_v0_3_0.json`
- `configs/transformations/sft1_value_first_v1/repr_six_goal_gate_execution_v0_3_1.yaml`
- `configs/transformations/sft1_value_first_v1/repr_six_goal_gate_v0_3_1.yaml`
- `configs/transformations/sft1_value_first_v1/repr_six_goal_gate_receipt_v0_3_1.json`
- `configs/transformations/sft1_value_first_v1/repr_six_goal_evidence_v0_3_1/manifest.json`
- `configs/transformations/sft1_value_first_v1/repr_six_goal_evidence_v0_3_1/01_mathlib_add_pow.json`
- `configs/transformations/sft1_value_first_v1/repr_six_goal_evidence_v0_3_1/02_physlib_kinetic_energy_conserved.json`
- `configs/transformations/sft1_value_first_v1/repr_six_goal_evidence_v0_3_1/03_cslib_ret_merge.json`
- `configs/transformations/sft1_value_first_v1/repr_six_goal_evidence_v0_3_1/04_lean_compiler_int_lt.json`
- `configs/transformations/sft1_value_first_v1/repr_six_goal_evidence_v0_3_1/05_canonical_gold_aime_1983_p1.json`
- `configs/transformations/sft1_value_first_v1/repr_six_goal_evidence_v0_3_1/06_consistency_check_amc12a_2019_p21.json`
- `tests/unit/sft1/__init__.py`
- `tests/unit/sft1/test_composition_policy.py`
- `tests/unit/sft1/test_repr_six_goal_gate.py`
- `configs/transformations/sft1_value_first_v1/wave1_gate_admission_v0_3_2.yaml`
- `configs/transformations/sft1_value_first_v1/clean_checkout_receipt_v0_3_2.json`
- `src/leanfaith/sft1/admission_readiness.py`
- `tests/unit/sft1/test_admission_readiness.py`
- `configs/transformations/sft1_value_first_v1/wave1_source_census_v0_3_2.yaml`
- `configs/transformations/sft1_value_first_v1/wave1_source_census_receipt_v0_3_2.json`
- `src/leanfaith/sft1/source_census.py`
- `tests/unit/sft1/test_source_census.py`
- `configs/transformations/sft1_value_first_v1/wave1_n31_guard_bank_v0_3_2.yaml`
- `src/leanfaith/sft1/n31_guard_policy.py`
- `tests/unit/sft1/test_n31_guard_policy.py`

**Exact paths claimed by the revision-0.3.3 session:**

- `plans/30_sft1_deterministic.md`
- `configs/transformations/sft1_value_first_v1/wave1_effective_readiness_v0_3_3.yaml`
- `src/leanfaith/sft1/effective_readiness.py`
- `tests/unit/sft1/test_effective_readiness.py`

The revision-0.3.3 paths are claimed only for the additive readiness overlay, its strict Lean-free
effective-state loader, and invariant tests. The revision-0.3.2 paths remain frozen evidence. No
shared contract, REPR file, shared TransformEngine, historical artifact, user-work path, or frozen
track is claimed, and no claimed path implements or executes a transform or generates rows.

**Exact paths claimed by the revision-0.3.4 session:**

- `plans/30_sft1_deterministic.md`
- `LeanFaith/Meta/SFT1/Wave1.lean`
- `configs/transformations/sft1_value_first_v1/wave1_operation_banks_v0_3_4.yaml`
- `configs/transformations/sft1_value_first_v1/wave1_implementation_readiness_v0_3_4.yaml`
- `src/leanfaith/sft1/wave1_readiness.py`
- `tests/fixtures/sft1/wave1_v0_3_4.yaml`
- `tests/unit/sft1/test_wave1_readiness.py`

These are additive task-owned implementation-readiness paths only. Revision 0.3.4 may author and
hash-bind the five primary Wave 1 mechanisms and their future dispatch/checker/cache/fixture
contracts, but no authored Lean source may be compiled or executed in this session. A source being
present and hash-bound is not a passed implementation, a live fixture, gate evidence, operation
promotion, label, or row. `N31_DROP_REQUIRED_GUARD_PROOF_V1` may be described only as an optional
sidecar-evidence adapter; it is not a sixth implementation-readiness blocker. All earlier claimed
paths other than this brief are frozen for this revision.

**Exact paths claimed by the revision-0.3.5 session:**

- `plans/30_sft1_deterministic.md`
- `configs/transformations/sft1_value_first_v1/p01_identity_policy_v0_3_5.yaml`
- `src/leanfaith/sft1/p01_identity_policy.py`
- `tests/unit/sft1/test_p01_identity_policy.py`

These are additive task-owned policy/loader/test paths only. Every revision-0.3.4 source, policy,
bank, fixture, loader, and test artifact is frozen. Revision 0.3.5 may clear only the named P01
identity blocker in its composed effective state; implementation readiness, gate execution, Lean,
transformation, row, production, Wave 2, 10K, scale, training, and publication states remain false.

The approved overlay at preserved parent commit `505b747…` is pinned at raw/typed-semantic hashes
`ee43bbbe00dc7f1063cb9dec334bfb204bcedb3bae255841e3b70c85470c2bf3` /
`a4aa3ddc383fdbc5fd1e161b5955f403ac17afa98f9d24defab4c2741846b4fd`. The strict loader and its
focused invariant suite have raw hashes
`8c6eff74bb2e0b590dad07cffea4542ecf21df997ce1588eb97087c3bb7b3e24` and
`0dbe59fdef816c9e995e48587f144013c98b0ff21f1bd3ccb79a2e02a8f1d14e`, respectively.

**Exact paths claimed by the corrective session over commit `505b747…`:**

- `plans/30_sft1_deterministic.md`
- `configs/transformations/sft1_value_first_v1/p01_identity_policy_v0_3_5.yaml`
- `src/leanfaith/sft1/p01_identity_policy.py`
- `tests/unit/sft1/test_p01_identity_policy.py`

The parent commit remains preserved in history. This correction may add only the named
composition/dedup runtime-binding blocker, its exact fail-closed contract, loader enforcement,
adversarial invariants, new content hashes, and this brief update. It may not implement or invoke
the runtime and grants no Lean, transformation, gate, row, production, Wave 2, 10K, scale,
training, publication, or push authority.

The corrected envelope is pinned at raw/typed-semantic hashes
`84b1ea8dcb3a302f4c4f92c7a82f5c68ddbf45655c060e588de0acce7453e01c` /
`dcdd6c07a83aa84faf81b448e2732121027b5a93fc89512caa38035b9c4cdbe4`. Its strict loader and
focused adversarial suite have raw hashes
`d322e615f2990fd35812a0af26fa43802938e9d62f6140d73293ed82d32b68ad` and
`31a8b82cd602a46292a7edf4460b165fc4a5e8f86f252ef925b4411c9c9c1d43`, respectively. The corrected
semantic hash is intentionally distinct from the approved runtime-input policy hash `a4aa3ddc…`;
the loader's exact pre-correction projection replays the latter.

**Exact paths claimed by the 72-hour sprint session:**

- `plans/30_sft1_deterministic.md`
- `LeanFaith/Meta/SFT1/Sprint.lean`
- `src/leanfaith/sft1/sprint/` (`__init__.py`, `inventory.py`, `engine.py`, `screens.py`,
  `store.py`, `runner.py`, `shortcut.py`)
- `configs/transformations/sft1_value_first_v1/sprint_v1.yaml`
- `tests/unit/sft1/test_sprint_lean_free.py`
- the sprint staging root `/storage/milikic/leanfaith/value_first/sft1_deterministic_v1/sprint_v1/`

The historical `Wave1.lean`, `ThinSmoke.lean`, every policy/readiness/admission/identity/census
loader, receipt, and YAML remain untouched; the sprint runner does not import or depend on them.

**Exact paths claimed by the additive two-row thin-smoke session:**

- `plans/30_sft1_deterministic.md`
- `LeanFaith/Meta/SFT1/ThinSmoke.lean`
- `configs/transformations/sft1_value_first_v1/thin_smoke_v1.yaml`
- `src/leanfaith/sft1/thin_smoke.py`
- `tests/unit/sft1/test_thin_smoke.py`
- `configs/transformations/sft1_value_first_v1/thin_smoke_v1_evidence/`

Every earlier SFT1 path is read-only in this session. The evidence directory may contain only the
two core rows, their keyed compact sidecars, and the hash-bound smoke manifest. Raw backend/cache
artifacts remain in the task-owned staging root. No coordinator-owned path is claimed.

## Coordinator requests

1. **REPR and SFT1 representation receipt resolved:** coherent freeze
   `176a783842c5a73b84413dfa8347670608b615d9` and the independent SFT1 6/6 direct-Expr receipt
   `f62b68ebc946469952bdd34674c127e2bd1146b0a8febbe5d199fea54a081e78` are pinned. This resolves
   representation consumability only and does not admit a transform or label. The attempt-008
   v0.3.0 artifacts remain preserved as superseded evidence.
2. **Shared-contract request remains open:** update `plans/00_shared_contracts.md` additively so an SFT1
   binary model-facing label requires exact row-local evidence plus production admission of the
   exact registered operation and its family/rubric dimension, together with separate row-emission
   authorization. Gate admission creates only bounded gate evidence. Polarity multiplication,
   generic D0, F2 direction, failed search, and candidate provability alone never create a label.
   Record N-RUBRIC and capped N-PROOF lanes and keep candidate truth evidence
   `proved|refuted|unknown` separate from the label.
3. **P01 identity/dedup request resolved at policy level:** the user's exact revision-0.3.5
   approval permits only the qualified adjacent P01 closed-Expr-hash equality defined above. The
   frozen revision-0.3.4 rule and blocker objects remain byte-identical; only the additive composed
   state clears that blocker. The corrective runtime-binding blocker remains open until the real
   composition/dedup runtime binds `a4aa3ddc…` and replays every acceptance, rejection, cap, and
   duplicate/conflict condition. This does not resolve implementation readiness and grants no
   execution or row authority.

No coordinator request authorizes this session to edit a coordinator-owned path. Policy revision 0.3.1
and additive revisions 0.3.3/0.3.4/0.3.5 leave `plans/00_shared_contracts.md` untouched.

## Closed REPR dependency and remaining pre-Wave-1-smoke blockers

- **arrived and pinned:** implementation/freeze commits; spec/config/Lean/injected-helper/Python
  hashes; implementation-set, renderer-semantic, and renderer-API hashes; universe-profile and
  render-context IDs/hashes; route/emitter/sidecar contract; reviewed SFT1 helper/project contexts;
  six complete reference/candidate pairs; `[anonymous]`/`⋯` rejection; complete sidecars; and the
  SFT1 coverage ID/hash/passed receipt;
- **measured result:** authoritative attempt 009 passed 6/6 pairs and 12/12 endpoints in 21.546
  Lean-seconds with 119,895 sidecar bytes, receipt `f62b68e…`; attempt 008 remains superseded
  evidence;
- **strict policy hardening complete:** the typed loader now fail-closes the separate
  reference/candidate/F0/F1/truth/F2/disposition axes, the exact six certificate-class partitions,
  all-operation binder and empty-domain profiles, environment fingerprint, closed N26/N31/N32
  design banks, correlation/effective-diversity groups, exact counters, surface-residue rules, and
  wave-local readiness;
- **admission and independent replay recorded:** the exact six operations and N31 dimension are
  gate-admitted by the user's adoption of Section 8. The clean detached checkout at approved
  commit `343ea088…` passed 127/127 focused tests plus the Git-local attempt-009 replay and the
  dedicated no-`/storage`-read check. This resolves admission and clean-checkout evidence, not
  implementation readiness;
- **still open before the Wave 1 smoke:** the P01 identity-exception composition/dedup runtime
  binding and replay; the additive shared label contract; exact dispatch and
  certificate-checker bindings for the five primary Wave 1 IDs; concrete binder/empty-domain
  checker results; their resolved anchors, applicability-bank implementations, and adversarial
  fixtures; root-specific hash-bound micro-censuses for the exact P01 and N31-RUBRIC roots; and the
  concrete closed N31 target-head/nonredundancy checker. The immutable 0.3.2 snapshot still records
  all four N31 proof routes as unknown, but revision 0.3.3 interprets that only as
  `not_in_scope_for_n_proof`: it cannot block the N31-RUBRIC smoke, its parent operation, or another
  project. P23 is not in Wave 1 and therefore its future regression does not block Wave 1; and
- **not acceptable as substitutes:** the passed representation receipt for any semantic certificate,
  operation admission, transform success, or production row.

Independent label-contract prerequisite: the coordinator merges and pins the additive shared rule
that exact evidence plus the required production family/dimension and exact-operation admissions
and row-emission authorization—not polarity multiplication—creates an SFT1 model-facing label;
gate admission creates bounded evidence only.

## Recorded Wave 1 decision and next decision boundary

The user explicitly adopted the GPT Pro review's Section 8 wording on 2026-08-30. The exact
approved wording is hash-bound in `wave1_gate_admission_v0_3_2.yaml` and reproduced here:

> **Approve SFT1 policy revision 0.3.1 at commit `343ea0885e24a5ea062034559b7e4df33db408b6` for Wave 1 gate admission of exactly `P01_ALPHA_RENAME_SINGLE_V1`, `P15_SWAP_IFF_SIDES_V1`, `P18_SYMMETRIZE_EQUALITY_V1`, `P21_BETA_REDUCE_V1`, `N31_DROP_REQUIRED_GUARD_RUBRIC_V1`, and `N31_DROP_REQUIRED_GUARD_PROOF_V1` across their registered eligible projects. Also approve gate admission of the N31 `required_domain_guard` family/dimension for those two N31 operations.**
>
> **This approval authorizes only task-owned implementation and, after the strict loader confirms all readiness prerequisites—including the coordinator-owned shared-label-contract update, the zero-Lean census and source-eligibility matrix, a clean-checkout policy/evidence replay, and complete hash-bound implementation, dispatch, certificate-checker, anchor, applicability-bank, fixture, and regression bindings for all six selected operations—the following bounded gates:**
>
> 1. **one actual serialized positive row and one actual serialized negative row end to end;**
> 2. **the selected-wave operation/project conformance matrix with one success and one expected adversarial rejection per registered combination; and**
> 3. **approximately 100 eligible roots per selected operation with 100% retained-certificate replay and the frozen counter/conservation report.**
>
> **The N31 admissions are proof-of-concept gate admissions only. This approval does not grant production admission to any operation, model-facing row emission, a 10K pilot, bulk generation, training, publication, or any source-root or row-count commitment. Passing any bounded gate does not promote an operation or authorize rows. Any production eligibility requires a separate exact post-report user decision naming the operation versions, projects, family/dimension, lane, hashes, axiom profile, measured receipt, and cap; any 10K pilot requires another separate approval.**

The user subsequently authorized only preparation and commitment of additive readiness revision
0.3.3 from checkpoint `dae99b3bd04d765a7a2011e10129589951dcb3c2`. The exact authorization is
stored in the 0.3.3 overlay under SHA-256
`fc0c951ebaf1c43c47c9582e0f6c8ca0769b40c1d6af0613d59556278d111e56`. It permits the Lean-free
policy/loader/test changes described here, preserves the earlier bounded Wave 1 admission, and does
not start implementation or execution, admit Wave 2, or grant any production or scale authority.

The user then froze checkpoint `18618ca6ff8383c5254bfacbfed2f4747daebbb7` and authorized this
additive revision 0.3.4 to prepare task-owned static implementation-readiness components for the
five primary mechanisms only. N31 N-PROOF remains optional and unimplemented. Revision 0.3.4 does
not inherit permission to run Lean or any gate from the older admission wording.

The user subsequently approved only the additive revision-0.3.5 P01 identity policy derived from
`5ddda95d05fe4c0fcd755e042174ca50453ebd03`. That decision clears the one P01 policy blocker under
the exact certificate-gated adjacent-hash exception above. It does not authorize Lean,
transformation or gate execution, model-facing rows, production admission, Wave 2, 10K, scale,
training, or publication.

The 0.3.5 conditional review then authorized only this Lean-free corrective commit. It preserves
`505b74754f881e903b5f04eab99311a125484b24` as the direct parent, keeps `a4aa3ddc…` as the future
runtime policy identity, and adds one open implementation blocker. It does not authorize a push,
live Lean work, runtime implementation or replay, transformations, gates, rows, production,
Wave 2, 10K, scale, training, or publication.

This remains bounded Wave 1 gate admission only. It does not make implementation ready, permit
Lean/transform execution before the named prerequisites, admit any operation to production, or
authorize a model-facing row. This session stops after the Lean-free corrective commit and does not
request live Lean authorization. A later instruction must separately authorize any project Lean
compilation or Meta validation; gate execution still cannot begin until readiness is true. After the
wave gates, separately request exact
operation-production promotions supported by the measured receipt. Only after those promotions and
the complete cross-source census are recorded may the project request exactly:

> Approve only the measured 10K SFT1 pilot described in the completed one-positive/one-negative smoke, selected-wave conformance, and approximately-100-roots-per-semantic-mechanism report and the completed cross-source census; do not approve bulk generation, scale, publication, or any production root or pair-count commitment.

## Session kickoff prompt

```text
Create a dedicated SFT1 worktree/branch from local-main coordinator commit
`c17104fe9bec1cb9eaf847c4e412aa0ca76c178a` (or a later coordinator descendant); do not edit the
integration checkout or continue from a pre-integration task tip. Own only SFT1. Read AGENTS.md, PLAN.md,
plans/00_shared_contracts.md, plans/72h_sft_data_sprint_2026-09-01.md, and this brief completely.
The 72-hour sprint override above is authoritative. Preserve the hash-bound historical Wave1
engine, policy/readiness/admission/identity/census loaders, receipts, and YAMLs; do not make the new
runner depend on them and do not add an authorization or census gate.

Implement one compact additive Mathlib engine for exactly P15, P18, P14, P23, proof-backed N25,
proof-backed N32, and proof-backed N31. If P14 takes more than two implementation hours or cannot
yield ten accepted pilot pairs, substitute P24. P01, P21, composition, rubric-only negatives, and
generic guard deletion stay off. Every positive retained row must carry a replayed,
Lean-typechecked `Iff reference candidate` proof. Every negative retained row must carry both the
loaded source-theorem proof and a replayed, Lean-typechecked `Not candidate` proof under a complete
ground context; failed or unavailable refutation is sidecar-only, never label 0. Restrict N32 first
to strict Nat/Int `<`, and N31 to bounded Nat/Int guard schemas with checked boundary witnesses.

Build the thin runner around stable root/operation terminals: preselect roots cheaply, read cache
before Lean, batch through one claimed persistent Mathlib worker with `Elab.async=false`, append one
durable terminal per root/operation, resume without repeating completed Lean work, and deduplicate
globally. Bind cache semantics to root closed-Expr hash, operation ID, engine version,
Lean/project revision, and import/options context; keep runner/config hashes only as provenance.
Reject self-pairs, invalid/open Exprs, wrong turnstile count, `[anonymous]`, `⋯`, and generated
dagger names on ordinary explicit locals. Keep minimal core rows and keyed sidecars.

Compile the engine, run one success and one typed rejection fixture per operation, then run 100
deterministic Mathlib roots and prepare 30 operation-stratified pairs for inspection, including all
N31 rows. If the recorded pass criteria above hold, automatically launch 10,000 retained pairs in
1,000-pair shards in a named detached tmux session; do not pause for another review or approval.
Run the shortcut screen after 10K and scale only if it, proof replay, deduplication, and the measured
completion projection pass. Report durable counts, cache/restart behavior, throughput, failures,
resource use, output paths, and exact remaining ETA. Do not run training.
```

## Progress log (append-only)

- 2026-08-30 — task brief created in `waiting_user`; no new transform approved or data generated.
- 2026-08-30 — Codex `/root` claimed only the three catalog-gate paths listed above. Began the
  read-only inventory/audit; no Lean job, LLM labeling, implementation, or data generation started.
- 2026-08-30 — completed the family-by-family audit and machine-readable composition proposal in
  the claimed config paths. YAML parsing and `git diff --check` passed. Returned to `waiting_user`;
  approval remains pending, and no Lean process, generation, staging, or publication was run.
- 2026-08-30 — integrated the user-supplied independent review into proposal revision 0.2.0.
  Added exact-closure and dependency/axiom firewalls, grounded negative semantics, `P-REFLECT`,
  typed-site rediscovery, P39--P42/N29--N32 proof-of-concept specifications, stronger shortcut
  controls, and the corrected 2--3M core/5M stretch scale contract. Status remains `waiting_user`;
  no implementation, Lean process, data generation, staging, or publication was started.
- 2026-08-30 — user explicitly withheld approval of revision 0.2.0 and requested revision 0.3.0.
  Claimed the four exact SFT1 policy-loader/test paths above for non-Lean contract enforcement only;
  no transform implementation, Lean process, row generation, staging, or publication was started.
- 2026-08-30 — completed revision 0.3.0 policy-only design with the reviewed `cbc933…` REPR
  predecessor non-consumable, its replacement fields open, exact family/dimension admissions,
  pack-only P23 hygiene, relation-only N32, deterministic sampling/dedup/orientation contracts, and
  active bank/blocklist bindings. The strict loader and 63 focused invariants, plan tests, Ruff, and
  Mypy passed. Status remains `waiting_user`; no approval, Lean execution, transform implementation,
  row generation, 10K pilot, scale, staging, or publication occurred.
- 2026-08-30 — user approved REPR freeze `176a783…` for SFT1 integration and conditionally
  authorized the six-real-goal gate followed, on complete success, by the existing one-example and
  approximately-100-root gates. Claimed the five exact six-gate paths above, pinned every supplied
  REPR identity in the machine contract, and kept SFT1 coverage null/false pending its own receipt.
  No registered transform, training row, 10K run, bulk scale, or publication was authorized.
- 2026-08-30 — final `attempt_008` passed and froze the independent SFT1 direct-Expr representation
  gate: 6/6 real-goal pairs, 12/12 endpoints, 8,118 ms aggregate, complete sidecars, required
  distinctness, exactly one turnstile per output, and no `[anonymous]` or `⋯`, under semantic
  receipt `7546ff3b5c0a0c2134c2581607662462ea3c19df412b61623033ac89f33544c5`. Earlier attempts
  failed closed on a misnamed Meta helper, an unqualified compiler constant, canonical-gold binder
  and delimiter issues, and legacy ConsistencyCheck big-operator syntax; none received a success
  receipt. The frozen repairs are confined to the reviewed helper/project contexts, and all six
  cases remain `production_admission: false`. This closes only the REPR representation dependency.
  Revision 0.3.0, registered-transform implementation, transform one-example/100-root gates, rows,
  10K, scale, and publication remain unapproved; the additive shared contract plus exact
  machine-schema/dispatch/certificate hardening precede the next exact user decision.
- 2026-08-30 — preserved attempt 008 as superseded evidence and completed the corrected additive
  v0.3.1 `attempt_009`: 6/6 real-goal pairs, 12/12 endpoints, 21.546 measured Lean-seconds, and
  119,895 persisted sidecar bytes under semantic receipt
  `f62b68ebc946469952bdd34674c127e2bd1146b0a8febbe5d199fea54a081e78`. The authoritative
  execution preimage is raw/effective hash `82f22c08…` / `dfc7037e…`; the post-pass binding is
  `5126eb8f…` / `7404e319…`. The correction pins the canonical-gold source/formal hashes and uses
  minimal source-faithful scopes, including no `TimeM` scope for CSLib. The user-authorized bounded
  one-example/adversarial and approximately-100-root scope remains readiness-closed until the
  shared contract, strict machine hardening, exact dispatch/checker/bank/fixture bindings and
  admissions, P23 regressions, and zero-Lean census resolve. No operation, F1 label, training row,
  10K pilot, bulk scale, production count, or publication was admitted.
- 2026-08-30 — completed the adversarial-review policy hardening without Lean: the strict loader now
  enforces the seven-axis row receipt, six exact certificate-class partitions, all 46 unresolved
  operation bindings as fail-closed, all-operation binder/empty-domain and environment profiles,
  closed and hash-bound N26/N31/N32 design banks, exact correlation/effective-diversity accounting,
  terminal conservation contracts, and the early model-facing residue diagnostic. The schema is
  complete; concrete dispatch/checker/anchor/bank/fixture resolutions remain prerequisites. No
  transform implementation, gate execution, row, 10K pilot, scale, or publication was started.
- 2026-08-30 — replayed the final Lean-free handoff checks: all 120 focused policy, durable-receipt,
  and project-plan tests passed; Ruff check and formatting, strict Mypy, and `git diff --check`
  passed; the live loader reproduced policy hash `23a2a936…`, exactly 46 registered operations,
  exactly 46 fail-closed unresolved execution bindings, and false started/authorized flags for the
  one-example, approximately-100-root, and 10K gates. No Lean process or row generation was run.
- 2026-08-30 — prepared policy revision 0.3.1 without Lean. It separates bounded implementation
  authorization, implementation readiness, selected-wave gate admission, production admission, and
  row-emission/scale authorization; proposes a six-operation Wave 1 while leaving the other 40
  fail-closed; records the 24-combination/48-fixture/~600-root Wave 1 cost and the superseded
  156-combination/312-fixture/~4,600-root full-matrix cost; restores the serialized positive/negative
  smoke → selected-wave conformance → approximately-100-root progression; and requires an explicit
  measured negative-operation production promotion. The Git-relative attempt-009 evidence bundle
  is pinned at manifest SHA-256 `aeb44673…`; the successful gate cases are correctly described as
  clean-residue results, not live forbidden-string injections. Drop receipts and the two exact
  rendered-placeholder failure mappings are now policy-enforced. Wave 1 gate admission remains the
  exact pending user decision, and the current freeze permits zero production negatives. Only the
  two claimed prose files were edited by this subtask; `git diff --check` passed. No Lean, transform
  execution, row generation, 10K pilot, scale, publication, or shared-contract edit occurred.
- 2026-08-30 — integrated the complete revision 0.3.1 policy, typed loader, adversarial invariants,
  and Git-relative attempt-009 evidence replay on review branch
  `milikic/sft1-v0.3.1-wave-policy-review`. All 127 combined SFT1 policy, representation-replay,
  and project-plan tests passed; Ruff check and formatting, strict Mypy, the live loader, evidence
  hash replay, and whitespace checks over every editable SFT1 path passed. The two newly checked-in,
  raw-hash-bound six-goal YAML preimages retain their frozen extra EOF blank lines rather than
  rewriting historical evidence. The live state remains 0 gate-admitted operations, 0
  production-admitted operations, 0 production negatives, no row emission, and no 10K
  authorization. `plans/00_shared_contracts.md` remains untouched and its additive label rule is
  still a coordinator request. No Lean, transform execution, row generation, scale, or publication
  occurred.
- 2026-08-30 — the user explicitly adopted Section 8 of the GPT Pro review for approved commit
  `343ea0885e24a5ea062034559b7e4df33db408b6`. This records gate admission of exactly P01/P15/P18/
  P21-beta-reduce and the N31 rubric/proof operations plus the N31 `required_domain_guard`
  dimension, limited to task-owned implementation and the bounded smoke/conformance/~100-root
  gates after all readiness prerequisites. Production, model-facing rows, 10K, bulk, training,
  publication, and count commitments remain closed. Began only a clean-checkout Lean-free receipt
  and fail-closed admission/readiness revision; no Lean or transform execution was started.
- 2026-08-30 — froze the additive Wave 1 admission/readiness overlay under raw/semantic hashes
  `c1cf0771…` / `8f50f382…`. The exact six operations and N31 dimension are gate-admitted, while
  gate execution remains false behind four blockers: the coordinator shared-label contract,
  completed source census/proof eligibility, the implemented closed N31 checker/target-head bank,
  and six complete execution-binding bundles. The detached approved-commit replay passed 127/127.
  The initial zero-Lean census contract remains explicitly incomplete and records every N31
  N-PROOF project route as unknown/ineligible. The five-shape N31 design now fail-closes unknown
  nonredundancy/reachability, separates live conformance from shape-regression coverage, and binds
  contradiction checks to exact roles, types, and instances. All 253 combined focused policy,
  representation, admission, census, N31, and plan tests passed; Ruff, formatting, strict Mypy,
  live loader/hash replay, and whitespace checks passed. No Lean/lake, transform execution, row
  generation, 10K, scale, training, publication, or coordinator-owned edit occurred.
- 2026-08-30 — user authorized additive readiness revision 0.3.3 from frozen checkpoint
  `dae99b3…`, limited to task-owned Lean-free policy, effective-loader, invariant, formatting, and
  plan work. The revision preserves 0.3.2 and every frozen hash/receipt; records six Wave 1 IDs as
  five semantic mechanisms; replaces the full-census smoke blocker with a hash-bound root
  micro-census; requires the selected-wave census before approximately 100 roots and the complete
  cross-source census before any 10K/count/scale/publication decision; makes N31 N-PROOF optional
  and nested under its parent root pool and cap; separates internal/pilot/release/publication source
  states; and records the 17-ID GPT Pro Wave 2 list only as `proposed_not_admitted`. No Lean,
  transform or gate execution, census-scale processing, row generation, Wave 2 implementation,
  production admission, 10K, scale, publication, or shared-contract edit was authorized or run.
- 2026-08-30 — completed the additive revision-0.3.3 overlay and strict effective-state loader under
  raw/typed-semantic hashes `5673d2ee…` / `1b323508…`. The focused new suite passed 92/92 and the
  combined Lean-free SFT1 loader/invariant plus plan suite passed 345/345; the four plan tests,
  Ruff check/format, strict Mypy, live loader replay, frozen-input hash checks, and whitespace checks
  passed. The loaded state preserves exactly 46 registry entries, six Wave 1 IDs/five semantic
  mechanisms, current dynamic accounting of 20 cells/40 fixtures/approximately 500 independent
  roots, zero production operations or negatives, and Wave 2 `proposed_not_admitted`. Only the
  brief and three new revision-0.3.3 paths changed. No Lean/lake, transform or gate execution,
  census-scale processing, row generation, production admission, 10K, scale, publication, push, or
  coordinator-owned edit occurred.
- 2026-08-30 — user froze checkpoint `18618ca6ff8383c5254bfacbfed2f4747daebbb7` and authorized
  only additive task-owned Wave 1 implementation-readiness work for P01 alpha rename, P15 Iff-side
  swap, P18 equality symmetry, P21 beta reduction, and the N31 required-guard mutation. Exact new
  source, policy/bank, fixture, loader, and test paths are claimed above. N31 N-PROOF remains an
  optional non-blocking evidence adapter. Lean/transform/gate execution, rows, Wave 2,
  production/10K, scale, and publication remain prohibited; the Lean bottleneck contract still
  requires all cheap schema, provenance, hashing, filtering, and sampling work before later bounded
  typed Meta validation in persistent workers, never one process per row.
- 2026-08-30 — completed the additive static revision-0.3.4 readiness layer without project Lean or
  gate execution. The uncompiled task-owned typed source is pinned at raw/import-stripped hashes
  `7d4c27e1…` / `0b905f3d…`; the operation bank at raw/semantic hashes `282836a5…` /
  `99440883…`; the 40-specification fixture matrix at `0856c6cf…` / `6d8dbc0d…`; and the main
  readiness contract at `87197cef…` / `cdf5ad55…`. The strict loader is pinned by this commit at
  raw hash `f59c9304…`. It loads exactly five primary bundles with implementation readiness false,
  preserves the complete 46-operation registry and every parent hash/receipt, admits zero runtime
  N31 bank identities, and keeps N31 N-PROOF optional/unimplemented. The new focused suite passed
  161/161; the combined Lean-free SFT1 loader/invariant and project-plan suite passed 506/506;
  Ruff check/format, strict Mypy, live typed-loader/hash replay, and whitespace checks passed.
  The separately recorded read-only `lean --print-prefix` boundary incident loaded no project and
  produced no artifact; no project Lean compilation, Meta validation, transform, gate, census,
  row, Wave 2, production, 10K, scale, or publication work occurred. P01's repeated alpha-invariant
  closed-Expr hash and N31's zero admitted resolved bank identities remain fail-closed blockers.
- 2026-08-31 — user approved an additive revision-0.3.5 P01 identity-policy overlay derived from
  frozen commit `5ddda95d05fe4c0fcd755e042174ca50453ebd03`. Claimed only the brief and three
  new task-owned policy/loader/test paths above. The approval can clear only the P01 identity
  blocker under the exact one-hop, cap, distinctness, rediscovery, certificate, unchanged-Expr, and
  deterministic-replay conditions; every other blocker and authorization boundary remains closed.
  Lean remains the bottleneck: this session performs only schema, hashing, provenance, loader, and
  invariant work before any later bounded persistent Meta validation. No Lean, transform, gate,
  census, row, production, Wave 2, 10K, scale, training, or publication work was started.
- 2026-08-31 — completed the additive revision-0.3.5 P01 identity-policy overlay without Lean or
  execution. The strict loader replays every frozen revision-0.3.4 dependency, binds the exact P01
  operation/bank/bundle and Git-local collision evidence, permits only the certificate-qualified
  adjacent closed-Expr equality, and removes exactly one blocker from the composed effective state.
  It also rejects bool/integer literal aliases so `true`, `1`, `false`, and `0` cannot cross typed
  policy axes. The focused suite passed 68/68 and the combined Lean-free SFT1 loader/invariant plus
  project-plan suite passed 574/574; Ruff check/format, strict Mypy, live loader/hash replay, frozen
  dependency replay, and whitespace checks passed. Policy raw/semantic hashes are `ee43bbbe…` /
  `a4aa3ddc…`; loader/test raw hashes are `8c6eff74…` / `0dbe59fd…`. Independent adversarial review
  reported no remaining findings. P01 implementation readiness, overall readiness, and gate
  execution remain false; N31, shared-label, cache/adapter, live fixture/replay, and smoke-census
  blockers remain open. No Lean, transformation or gate execution, census, model-facing row,
  production admission, Wave 2, 10K, scale, training, publication, or shared-contract edit occurred.
- 2026-08-31 — the 0.3.5 P01 policy review returned a conditional pass and authorized exactly one
  Lean-free corrective revision over preserved commit `505b74754f881e903b5f04eab99311a125484b24`.
  Claimed only the same four task-owned policy/loader/test/brief paths. The correction adds the
  explicit fail-closed blocker
  `p01_identity_exception_composition_dedup_runtime_binding_and_replay`, which must remain open
  until the real composition/dedup runtime binds policy semantic hash `a4aa3ddc…` and replays the
  exact adjacent-hash exception, rejection cases, cross-polarity/composition caps, and canonical
  duplicate/conflict rules. Lean remains the bottleneck: only schema, hash binding, loader, and
  adversarial invariant work may proceed. No Lean, runtime, transform, gate, census, row, push,
  production, Wave 2, 10K, scale, training, or publication work was started.
- 2026-08-31 — completed the conditional-pass correction without Lean or runtime execution. The
  corrected envelope adds exactly one open blocker,
  `p01_identity_exception_composition_dedup_runtime_binding_and_replay`, and an exact false
  incomplete prerequisite while keeping only `p01_alpha_closed_expr_hash_collision` cleared. The
  loader separately binds the preserved parent commit/tree/raw hash, future runtime semantic hash
  `a4aa3ddc…`, and corrected envelope hash `dcdd6c07…`; removing only correction-owned fields
  reconstructs and hashes the approved policy exactly. Runtime path, symbol, code hash, observed
  policy hash, receipts, and replay axes remain null/false. The focused suite passed 127/127 and the
  combined Lean-free SFT1 loader/invariant plus project-plan suite passed 633/633; Ruff
  check/format, strict Mypy, typed loader/hash/projection replay, frozen-dependency replay,
  project-plan validation, and whitespace checks passed. Independent adversarial review reported
  no findings. P01 implementation readiness, overall readiness, and gate execution remain false.
  No Lean, runtime, transformation, gate, census, row, push, production admission, Wave 2, 10K,
  scale, training, publication, or shared-contract edit occurred.
- 2026-08-31 — the user superseded the generalized readiness sequence with an empirical two-row
  thin smoke. The entire unapproved v0.3.6 worktree state was preserved without cleanup on pushed
  archival branch `milikic/sft1-v036-readiness-wip-archive` at commit `831e348d048cef3f7e143b178b17d6c46f0445d0`
  (tree `18ae5d6edf3a3fcd47b11653bc78edea45468b81`). This separate branch starts exactly at accepted
  commit `fc8cdc2c6d9d93e99e20933a17dbcfa2afc2be48` and claims only the thin helper, config,
  adapter/runner, focused test, brief, and two-row evidence bundle named above. Lean remains the
  bottleneck: one persistent claimed Mathlib worker will serve both rows, and cache replay must add
  zero requests. No census, P01, generalized N31 bank, Wave 1 gate, extra root, production, 10K,
  scale, training, or publication is authorized.
- 2026-08-31 — the thin implementation was committed and pushed at
  `142c7480ea1c6879b146b3084178ca7244d1c095` (tree
  `c114598733c78d48857ebb9062235c8c9bf5a859`) after 16 focused tests passed with one live-evidence
  skip, plus strict typing, lint, formatting, bytecode, config-loader, and plan checks. Two
  independent read-only audits found no definite Lean, REPR, cache, or replay blocker. The live
  command was attempted only through its resource claim and stopped before backend construction:
  `SFT2A-V5-2-REHEARSAL-CORRECTED-V4` still owned 2 workers/40 GiB, so the atomic ledger rejected a
  requested total of 3 workers against the cap of 2. At the two-hour cutoff that healthy external
  run had completed 51/100 roots. SFT1 made zero Lean requests, created no staging/evidence path,
  emitted zero rows, and took no reservation. Status is `blocked` only on release of that external
  capacity; the smallest resume is the already committed one-command smoke, with no redesign.
- 2026-09-01 — the coordinator rechecked the shared ledger, found no active reservation, and ran
  the exact authorized two-row command. The single live request failed closed before row or
  sidecar emission. It exposed four previously uncompiled defects in the injected Wave 1 source:
  Option-valued helpers used bind syntax outside `do`, `local` and `matches` were parsed as reserved
  tokens, and the thin helper projected unavailable `UInt64.toString`. Only one raw failure record
  was persisted; no evidence directory or training artifact was created, and the SFT1 reservation
  was released. The corrective path now injects only a narrow task-local P18 implementation plus
  the exact N31 canary, keeps the frozen Wave 1 source hash as design provenance, converts proof
  hashes to `Nat`, and remains bounded to the same two rows and replay.
- 2026-09-01 — the first corrective rerun confirmed that removing the uncompiled Wave 1 preamble
  eliminated every Wave1 parse failure and that both custom transforms reached elaboration. It
  still failed closed before rows because field projection also cannot resolve `Nat.toString` in
  this request environment. The follow-up uses the generic `toString` function explicitly; the
  failed raw response remains preserved and the reservation was again released.
- 2026-09-01 — the next rerun reached P18 and exposed a reusable Wave 1 correctness defect:
  `isDefEq` was called on equality operands containing loose bvars below the closed telescope,
  causing a Lean panic. The smoke correction relies on its existing structural nondegeneracy test
  and removes that unsafe redundant call. The general Wave 1 implementation must instead open its
  telescope to fvars before any definitional-equality comparison. No rows were emitted, the raw
  failure is retained, and a passing thin smoke still cannot certify Wave 1 readiness.
- 2026-09-01 — after the loose-bvar repair, frozen REPR correctly rejected the original
  `Nat.lor_comm` root because its `|||` notation is an unsupported compound bar operator. This is a
  root-eligibility failure, not a renderer bug and not permission to weaken REPR. The smoke replaces
  only that positive root with the exact source-pinned `PNat.gcd_comm`, whose final equality uses no
  unsupported surface notation; the operation, row count, and all authorization limits are unchanged.
- 2026-09-01 — the exact additive thin smoke passed at implementation commit
  `5199fe1a040d1c1a6b37d6b9c03b493963797920` (tree
  `ad84f9b5ff2db786277bcee62810d596a7fa28b5`). Run
  `16038ae99abb68f262b70b1aa5493ce7d0338ca6e48e73868271e7f7e8e36ae5` issued one persistent
  Lean request (`2cd997c4…`), retained exactly two rows, used 8.960906 seconds wall / 8,723 ms
  reported Lean time with 7,656,505,344 bytes peak process-tree RSS, and released its resource
  claim. The positive row is the exact P18 equality-side swap of the real Mathlib theorem
  `PNat.gcd_comm`; its distinct closed endpoints have a replayed transform certificate and a
  kernel-checked equivalence proof. The negative row removes the required `n = 0` guard from
  `n + 1 = 1`; its exact N31 canary certificate replayed, the reference proof was kernel checked,
  and the candidate was refuted at witness `n = 1`. Cache replay hit both entries and issued zero
  Lean requests. The checked-in evidence hashes are manifest `de170a9d…`, rows `41f7465b…`, and
  sidecars `da42e469…`.
- 2026-09-01 — the passing artifact is deliberately scoped as thin plumbing evidence. Its manifest
  records `thin_task_local_p18: true`, `wave1_engine_live_evidence: false`,
  `general_n31_bank_activated: false`, and `production_or_scale_authorized: false`. It therefore
  does not cure or certify the original `Wave1.lean` source: that engine still needs real project
  compilation plus correctness repair, including opening telescopes before P15/P18 definitional-
  equality checks. The next bounded step is live mechanism success/rejection and certificate replay
  followed by a manually inspected 10--20-real-root audit; no approximately-100-root gate,
  model-facing data, production admission, 10K, scale, training, or publication is authorized.
- 2026-09-01 — the user directed a 72-hour execution reset after GPT Pro and Fable 5 code review.
  The active sprint now bypasses, but preserves, the historical admission/readiness ceremony and
  uses a compact seven-operation, single-hop Mathlib engine with proof-backed negatives only. Its
  direct path is fixtures -> 100 roots plus 30 inspected pairs -> automatic sharded 10K on pass;
  the shortcut screen gates only larger scale. No Lean or generation was started by this plan edit.
- 2026-09-01 — sprint session (Claude Fable 5.1) created worktree `/localhome/milikic/LeanFaith-sft1-sprint`
  on branch `milikic/sft1-sprint-72h` from coordinator commit `5de43eb` (descendant of `c17104f`).
  Built the Lean-free Mathlib inventory (180,415 `theorem`/`lemma` declarations, 180,400 unique
  names, `inventory_sha256 73c98dfc…`) and the compact additive engine
  `LeanFaith/Meta/SFT1/Sprint.lean` (`engineSemanticVersion sft1_sprint_engine_v1`): P15/P18 final-
  target swaps, P14 adjacent independent explicit data-binder swap, P23 adjacent proof-independent
  hypothesis packing with the `h`/`h_<n>` hygiene rule, N25 Eq/Ne toggle, N32 strict `Nat`/`Int`
  `<` role swap, and N31 bounded literal-guard removal (`lit_lt_var`, `var_lt_lit`, `lit_le_var`,
  `var_le_lit`, `var_ne_lit`, `var_eq_lit` schemas). Every positive carries an `Iff` witness
  checked by `Meta.check` and independently by `Kernel.check`/`Kernel.isDefEq`; every negative
  carries the loaded source constant (kernel-checked against the reference) plus a `Not candidate`
  proof under a complete ground assignment found by bounded DFS over `Nat`/`Int`/`Bool`/`Prop`/
  `Type` values, synthesized instances, and `decide`/`omega`/`norm_num`/`simp` hypothesis proofs;
  N31 refutes the grounded target at the guard boundary. Universe parameters are instantiated at
  level zero for the kernel pass and recorded. Terminal classes are `retained`, `not_applicable`,
  `rejected`, `error`; `[anonymous]`, `⋯`, ordinary-local `✝`, self pairs, gold near-duplicate
  hits, duplicate unordered pairs, and render/text/hash mismatches are rejected in Python.
- 2026-09-01 — runner design: roots are interleaved deterministically from a `nat_int` pool
  (`Mathlib.Data.Nat.*`, `Mathlib.Data.Int.*`, weight 3) and the `general` pool (weight 1) by
  salted hash order; each batch of 25 roots is one persistent-worker *process* request (typed
  terminals plus pre-rendered texts) followed by one *render* request through the frozen
  `render_closed_expr_in_session` route with two `emitClosedProp` calls per pair, cross-checked by
  the engine's structural hashes and exact text equality. Incremental prefix reuse keeps follow-up
  requests at about 0.8 s. The append-only journal holds one terminal per root/operation; the
  semantic cache keys operation records by root structural hash, operation ID, engine semantic
  version, Lean/project revision, and import/options fingerprint (runner/config bytes and request
  hashes are provenance). Implementation commit `0f6ab91`; fixes in `fed94d1`.
- 2026-09-01 — fixture gate passed: all 14 fixtures (one success and one typed rejection per
  operation) in run `fixtures-274ea10f55b8`, 20 retained pairs, 2 Lean requests, 12.5 s wall,
  8.09 GB peak RSS.
- 2026-09-01 — 100-root gate passed (run `roots100`, first 100 ordered roots): 147 retained pairs
  in 32.7 s wall, 9 Lean requests, 8.21 GB peak RSS; retained by operation P15 15, P18 47, P14 21,
  P23 19, N25 28, N32 3, N31 14 (six mechanisms at ten or more, four positive and two negative);
  700 terminals = 147 retained / 524 not applicable / 29 rejected (22 N25 and 1 N32
  `no_ground_assignment`, 2 N31 `no_boundary_refutation`, 4 reference `⋯` residue rejections).
  Replay with the recorded limits issued 0 Lean requests and appended 0 rows. The 30-pair
  operation-stratified inspection (`runs/roots100/inspection/sample.md`, all 14 N31 rows
  included) found 0 wrong labels; verdict recorded in `inspection/verdict.json`.
  `gate_report.json` passed all ten recorded checks.
- 2026-09-01 — automatic 10K launch per the sprint contract: tmux session
  `leanfaith-sft1-sprint-tenk`, pane PID 2416695, python PID 2416707, started 2026-09-01T22:53:40Z
  from commit `fed94d1`, command `uv run python -m leanfaith.sft1.sprint.runner run --run-id tenk
  --target-retained 10000`, log `sprint_v1/logs/tenk.log`, journal `sprint_v1/runs/tenk/journal.jsonl`,
  status `sprint_v1/runs/tenk/status.json` (final marker `"final": true`). Health check at
  22:57:31Z: 350 roots considered (100 from cache, 250 via Lean), 515 retained, 133 retained/min,
  ETA about 71 min. Resume command: the same `run` command (completed terminals are skipped);
  read-only status: `uv run python -m leanfaith.sft1.sprint.runner status --run-id tenk`; attach:
  `tmux attach -t leanfaith-sft1-sprint-tenk`. Stop conditions: a potentially wrong core label,
  a non-resumable failure, throughput below the ETA window, or loss of the shared Lean allocation.
- 2026-09-01 — while the 10K run proceeds, added the release tooling at commits `519a66c`…`21d9acc`:
  `gate10k` (ancestry-grouped 1,000-pair shards, 100% proof-check verification, duplicate/conflict
  rejection, candidate-only/reference-only/mechanism-held-out shortcut screens with stratified
  cluster-bootstrap upper bounds, full-wave completion projection), `compact-windows` (root-order
  windows that become independently publishable shards only when every root in the window has
  all seven terminals, with cross-window duplicate suppression), and `publish` (additive private
  Hub commits with fresh-download hash verification and receipts). The publisher was exercised on
  the 100-root gate evidence: `Lemmy00/leanfaith-sft1-deterministic-v1` (private) revision
  `4fe06d22eeb5fc02340f9d58a110936b992222c6`, prefix `sprint_v1/roots100/` (147 rows, gate
  evidence only, not a release). Pre-rendered texts are now surface-validated with the frozen REPR
  canonicalizer before the render request (commit `fba4298`), so the full wave avoids the
  batch-atomic render failures and the false `render_reference_text_mismatch` rejections observed
  in the live 10K job, which still runs the earlier code.
- 2026-09-01 — the first `tenk` process exited at 23:25:59Z with an uncaught `GoalV1Error`
  (`unsupported or unterminated single-quoted target syntax`) after 3,430 roots and 4,695 retained
  pairs (1,936 s wall, 1,356 Lean requests). Cause: root names were passed to the render request
  as Lean name literals, and the frozen route's literal masker treats a pair of primed names
  (`foo'`, `bar'`) in one body as an unterminated character literal. No label evidence was
  affected; the journal and retained records are intact. Fix at commit `bccfde4`: names now travel
  as Lean string literals parsed by the engine's `parseName`, and any render-route error becomes a
  per-pair `rejected:render_failed:route:…` terminal. Fixtures re-passed 14/14
  (`fixtures-ff8f7dfaa3d4`). The resumable job was relaunched in the same tmux session
  `leanfaith-sft1-sprint-tenk` at 23:28:56Z (pane PID 2491224); completed terminals are skipped
  from the journal, so the restart repeats no finished Lean work.
- 2026-09-01 — the second `tenk` process exited at 23:45:34Z with a semantic-cache conflict: two
  Mathlib theorems with alpha-identical statements (aliases) shared the reference-hash operation
  key while negative evidence cites the specific source constant. Fix at commit `cb535fc`: the root
  name is bound into operation keys (cache schema 2). Relaunched at 23:47:56Z (pane PID 2559822).
- 2026-09-02 — 10K run `tenk` complete at 23:51:07Z (2026-09-01): 10,001 retained pairs over
  8,723 roots (8,618 via Lean, 105 from cache), 61,830 terminals (10,001 retained / 47,291 not
  applicable / 3,524 rejected / 14 error), retained by operation P15 1,362, P18 3,832, P14 1,939,
  P23 1,206, N25 1,236, N32 80, N31 346. Rejections: 2,909 `no_ground_assignment`, 412 reference
  residue (`⋯`/dagger), 111 `no_boundary_refutation`, 69 render-route failures, 9 self-pair text,
  5 duplicate-in-run, 5 candidate residue, 4 render-text mismatch; the 14 errors are two roots
  whose guillemet-escaped names round-tripped differently. Measured throughput with the fixed
  runner: 307 roots/min and 327 retained pairs/min on one worker (third process), peak RSS 10.0 GB.
  Replay with the recorded limits issued 0 Lean requests and appended 0 rows. A 346-row all-N31
  inspection sample was generated and 35 rows were read by hand with 0 wrong labels.
- 2026-09-02 — 10K release gate (`gate10k`): raw view 10,001 rows (8,339 positive / 1,662
  negative, 6,478 roots, 10 ancestry-grouped shards, 0 duplicates, 0 conflicts, 100% kernel- and
  Meta-checked evidence, two useful negative mechanisms) and per-root balanced view 2,922 rows
  (1,461 per label, 1,394 roots). Shortcut screens (hashed bag-of-bigrams logistic regression,
  root-clustered 5-fold, 95% cluster-bootstrap upper bounds): raw candidate-only 0.899 (UB 0.907),
  reference-only 0.805 (UB 0.815), mechanism-held-out 0.620 (UB 0.634, passes); balanced
  candidate-only 0.899 (UB 0.909), reference-only 0.500, mechanism-held-out 0.235. Both views
  therefore FAIL the candidate-only screen and the raw view also fails reference-only. Diagnosis:
  negatives arise only where refutation is possible (84.8% of raw negatives from the `nat_int`
  pool versus 42.1% of positives; 16% of negatives have generic `inst✝` locals versus 53% of
  positives), which per-root balancing removes; but 71% of raw negatives (79% balanced) carry the
  N25 `≠` toggle in the candidate versus about 1% of positives, and N31 candidates lack a guard
  hypothesis that every same-root positive keeps, so a candidate-only classifier separates labels
  without reading the reference. Excluding N25 `eq_to_ne` still leaves candidate-only 0.80 on the
  balanced remainder (N31 dominates). Projection: the full wave (171,677 remaining roots) would
  take about 9.3 h at the measured rate and fits the window, but the contract allows scale only
  when the screens pass, so no full wave was launched. Both views were published as private gate
  evidence, explicitly marked not-a-release in their cards: raw `sprint_v1/tenk` at
  revision `37d8401a038184c5b40534c4f3e7644c346c5236`; balanced `sprint_v1/tenk_balanced` at revision `0a4bbc8eee89dd9b82883a203c6cf00d6059d566`.
- 2026-09-02 — decision request (status `waiting_user`). The seven single-hop operations cannot
  pass a 0.60 candidate-only bound because every negative except N32 has a candidate-side
  signature with no positive twin. Options, cheapest first: (1) accept the 100%-checked 10K raw
  and balanced artifacts as the sprint deliverable and record the screens as a known limitation of
  single-hop symmetric operations; (2) add twin operations that neutralize the signatures, e.g. a
  disequality-symmetry positive (`a ≠ b` ↔ `b ≠ a`, `Ne.symm` witness) with root-count balancing
  between `=`-roots (P18 + N25 `eq_to_ne`) and `≠`-roots (twin + N25 `ne_to_eq`; the inventory
  has 2,099 `≠` conclusions versus 86,907 `=`), plus a cap on N31's share, then re-run the 10K
  gate; (3) redefine the shortcut screens for this data (e.g. candidate-only measured after the
  50% orientation swap, which gives 0.69/0.69 on the balanced view) and, if accepted, launch the
  full wave (about 9.3 h) with `compact-windows` and `publish --windows`. All runner, cache,
  journal, compaction, and publisher tooling is committed and tested; the wave can start within
  minutes of a decision.
- 2026-09-02 — provenance and integrity corrections (commits `9c4b81f`…`93e58af`), applied
  without touching the certified 10K rows:
  - Compaction no longer copies the first-launch run manifest. It derives every engine source
    SHA-256, compile-context identity, cache-key schema, and implementation segment from the
    sidecars and refuses inconsistent identities. The 10K (`tenk`) legitimately spans three
    segments: engine `274ea10f55b8…` / compile context `ctx:6934ba1b…` / cache schema 1 for
    4,695 rows (2,699 roots; engine commit `0f6ab91`), engine `ff8f7dfaa3d4…` /
    `ctx:cfe2c8f0…` / schema 1 for 4,269 rows (3,049 roots) and schema 2 for 1,037 rows
    (730 roots; engine commit `bccfde4`). All rows share one engine semantic version
    (`sft1_sprint_engine_v1`), one frozen REPR implementation identity, and one project pin set.
    Runner commits are not durable per row for these segments (only the engine-commit range is);
    new sidecars now carry `implementation_commit`, `runner_source_sha256`, and `cache_schema`.
  - A Lean-free integrity validator (`leanfaith.sft1.sprint.integrity`) checks row/sidecar joins,
    shard and record hashes, label polarity, evidence flags, render-hash and pair-id recomputation,
    residue screens, unordered-pair uniqueness, shard conservation against the manifest and the
    retained records, final run status, replay receipts, and sidecar-derived provenance. It passed
    on `tenk` raw (10,001 rows, 10 shards), `tenk` balanced (2,922 rows), and `roots100` (147 rows)
    with zero issues; reports live in each compacted directory as `integrity_report.json`.
  - Proof checks occurred during original generation. The recorded zero-Lean-call replays are
    journal/cache replays of stored terminals and cached artifacts, not fresh kernel replays; the
    manifests, release reports, dataset cards, and validator now say so explicitly
    (`proof_check_time: original_generation`,
    `replay_semantics: journal_and_cache_replay_of_stored_terminals_no_fresh_kernel_replay`).
  - The raw and balanced dataset cards are corrected to "diagnostic gate evidence, not a training
    release" (`artifact_status: diagnostic_gate_evidence_not_a_training_release`), with the
    segment table and the failing screen values on the card.
  - Durable verdict for the inspected N31 sample: `runs/tenk/inspection/verdict.json` records the
    346-row all-N31 sample hash, that rows 1–35 were read by hand with 0 wrong labels, and that
    the remaining 311 rows rest on their kernel-checked refutations only.
  - Overwritten performance evidence is marked unavailable rather than reconstructed:
    `runs/roots100/performance_evidence.json` (the 100-root `status.json` was overwritten by a
    replay before replay status was separated; `gate_report.json` now reports its wall time, Lean
    request count, and RSS as null with `performance_evidence: unavailable_overwritten_status`).
    `runs/tenk/status_provenance.json` records that `tenk/status.json` was restored verbatim
    from the third process's own exit summary line in `logs/tenk.log` and that its throughput
    describes only that process. Replays now write `replay_status.json`.
  - Regressions: resumed runs with multiple valid engine/compile-context/cache-schema segments
    are accepted and recorded; mixed engine semantic versions are rejected.
- 2026-09-02 — shortcut-corrected v2 (commits `63e764a`…`63c169f`, all pushed to
  `origin/milikic/sft1-sprint-72h`). Engine operation-set version 2 adds `P_NE_SYMMETRIZE_V1`
  (`a ≠ b ↔ b ≠ a`, `Ne.symm` witness, Meta- and kernel-checked) and the budgeted
  `P_DROP_REDUNDANT_GUARD_PROOF_V1` (drop an explicit hypothesis only when
  `assumption`/`omega`/`positivity`/`simp_all` proves it from the preceding context, complete
  `Iff` constructed and kernel-checked). Existing operations are byte-identical in behaviour;
  runs record their operation set and replay with it. Fixture gate: 17/17 with the
  redundant-guard success fixture explicitly waived in config after its yield run.
- 2026-09-02 — targeted additive runs (no regeneration of the 10K; cached operations reused
  per operation): `v2_ne` over the 2,099 preidentified disequality roots with only
  `P_NE_SYMMETRIZE_V1` and N25: 1,089 P_NE and 299 N25 `ne_to_eq` pairs in 802 s (168 Lean
  requests), 754 N25 `no_ground_assignment` rejections; replay 0 Lean calls. `v2_lt` over the
  3,874 strict-order roots with all nine operations: 2,332 pairs (N32 149, N31 92, N25 89, P14
  366, P18 379, P23 1,256, P15 1) in 755 s across two processes; replay 0 Lean calls. The first
  `v2_lt` process stopped on a cache conflict when a root shared with the 10K was recomputed in
  full and its P18 proof-term fingerprint differed (session-scoped auxiliary names); fix at
  `e6a21b1`: cached operations are reused individually and only missing operations go to Lean,
  and evidence comparison ignores proof fingerprints while requiring identical check results.
  `v2_guard` (redundant-guard positive over the 8,690 10K roots): 0 retained of 8,690 in 567 s
  (`runs/v2_guard/budget_verdict.json`) → stopped under budget; the N31 cap stays.
- 2026-09-02 — `core_v2` view (`compacted/core_v2`, sources `tenk`+`v2_ne`+`v2_lt`; 13,721
  input records, 335 cross-run duplicates removed, 0 conflicts): deterministic matched 2x2 relation
  design equalized by stable root hash — P18 Eq→Eq positive and N25 Eq→Ne negative from 212
  equality roots, `P_NE` Ne→Ne positive and N25 Ne→Eq negative from 212 disequality roots
  (212 = every disequality root with a grounded refutation; 1,291 equality roots were available)
  — plus 54 N32 negatives with same-root surface-neutral positive twins (`order` family) and 19
  N31 negatives with twins (`guard` family, 2% cap). 994 rows, 497 positive / 497 negative, 497
  roots; orientation randomization stored in the rows (501 swapped / 493 original) and recorded
  per sidecar. `aux_n31_core_v2` keeps all 389 certified N31 rows (not model-facing). Screens on
  the stored view with side-tagged pair features and polarity-paired surface-family held-out
  groups: candidate-only 0.562 (95% upper bound 0.581), reference-only 0.551 (0.570),
  family-held-out 0.483 (0.490) — all pass. Integrity validator: 994/994 and 389/389 rows, zero
  issues, provenance segments recorded (engines `274ea10f…`, `ff8f7dfa…`, `c81c2a60…`; cache
  schemas 1 and 2). Manual reading of a 30-row family-stratified sample: 0 wrong labels
  (`compacted/core_v2/verdict.json`); `runs/v2_ne/inspection/verdict.json` records 22 hand-read
  rows with 0 wrong labels. The remaining unmet release criterion is the ≥100-rows-per-negative-
  operation count (N25 424, N32 54, N31 19); no threshold was changed. The existing full wave
  was not launched: every `≠` and `<` root is already processed, so a wave would not enlarge the
  matched core; the lever is grounding coverage for disequality roots (754 ungrounded N25
  attempts).
- 2026-09-02 — private Hub state (`Lemmy00/leanfaith-sft1-deterministic-v1`, every card marked
  "diagnostic gate evidence, not a training release" or "candidate model-facing view"): corrected
  cards and top-level index at revision `f44d4b261492c1d112b3ac9f2a77c2df5114b2fe`;
  `sprint_v1/core_v2` (994 rows, with `integrity_report.json` and `verdict.json`) at
  `799c4ff8f13f5b55b6a58f700cf876e749b87dc8`; `sprint_v1/aux_n31_core_v2` (389 rows) at
  `4e2571f7dd6b7708632e25087c817aadaf7d92ed`; refreshed cards/index at
  `315b7988651d5ab3cae73ab24fc38a5314c79de3`. Every upload was verified by fresh download and
  has a local receipt under its compacted directory.
- 2026-09-02 — additive corrected seed release attempt (`core_v2_seed`, commits after
  `b40fdbb`; `core_v2` and `aux_n31_core_v2` untouched):
  - Shortcut evaluation is now order-invariant: rows are canonically ordered by pair id, the
    classifier is full-batch class-weighted L2 logistic regression (no minibatches, no random
    state), folds are stable SHA-256 root folds, and the 95% upper bounds come from a
    family-stratified root bootstrap (400 resamples). A regression shuffles the rows with three
    seeds and requires bit-identical metrics. The screens read the exact serialized shards.
  - Exact deterministic orientation: exactly one swapped row per paired root (497 swapped / 497
    original), selected by a salted root hash; model-facing rows are exactly
    `{reference, candidate, label}` and every identifier lives in the line-aligned sidecar
    (`pair_id`, `root_id`, `operation_id`, `mechanism`, orientation, family, cell). The finalized
    shard is marked complete. Mechanism metadata now comes from an exact table
    (`P_NE_SYMMETRIZE_V1` → `PNE`, `P_DROP_REDUNDANT_GUARD_PROOF_V1` → `PDRG`); the validator
    checks it, compares the full canonical provenance object, and verifies the orientation rule
    and finalized-shard completeness; regressions cover all of these. Cards are generated from
    the actual gate checks with no hardcoded shortcut wording.
  - Diversity rule applied proportionally: at least two negative mechanisms with at least
    `min(100, ceil(0.05 × rows))` = 50 rows; N25 424 and N32 54 qualify, capped N31 (19) is
    exploratory. This check passes.
  - Stable gate on the serialized `core_v2_seed` (994 rows, 497 roots, 497/497 labels):
    candidate-only 0.624 (upper bound 0.647) FAIL; reference-only 0.584 (0.605) FAIL;
    family-held-out 0.483 (0.492) pass. Per-family candidate-only balanced accuracy:
    eq_relation 0.573, ne_relation 0.672, order 0.639, guard 0.605; reference-only: eq_relation
    0.580, ne_relation 0.611, order 0.537, guard 0.447. Label-permutation control (two seeds):
    candidate-only 0.521/0.552 (upper bounds 0.554/0.581), reference-only 0.533/0.537
    (0.565/0.567) — the actual values sit above the control band, so the leak is real; the
    earlier `core_v2` "pass" (0.562/0.551) came from the order-sensitive minibatch screen.
    Integrity validator: 994/994 rows, zero issues (`compacted/core_v2_seed/integrity_report.json`,
    `release_report.json`, `permutation_control.json`). The seed was NOT published and is kept
    locally as gate-failed evidence (`artifact_status: candidate_seed_release_gate_failed`).
  - Status `waiting_user`. Proposed composition correction (no N25 grounding work): add
    certified negative mechanisms whose candidates do not carry a relation-symbol or
    hypothesis-count signature, and N32-positive twins that share N32's surface, e.g. (a) a
    strict-order side swap that is *positive* under an extra hypothesis is impossible, but a
    P-twin "swap independent data binders" already exists — expand N32's twin pool by targeting
    `<` roots with two explicit data binders; (b) an N-mechanism "swap hypothesis roles" (exchange
    two same-typed hypothesis arguments, refuted via the loaded proof) whose candidate keeps every
    token of the reference; (c) an N-mechanism "shift a literal bound by one" restricted to
    decidable Nat/Int targets, refuted at the boundary, whose candidate differs by one numeral.
    Each needs a fixture pair, a targeted run, and the stable serialized-row gate before any
    seed release.

- 2026-09-02 — `core_v3_square` correction executed (additive; `core_v2`, `core_v2_seed`, the 10K
  evidence, and all published prefixes untouched; no threshold, salt, grounding, or new-mechanism
  changes). Branch `milikic/sft1-sprint-72h`, commits `8d4d7f0`..`78475ff`.
  - Construction (`SQUARE_N25_SYMMETRY_V1`, engine `LeanFaith/Meta/SFT1/Sprint.lean`, source sha
    `34e2f6110abbc2b4…`, semantic version unchanged `sft1_sprint_engine_v1`; runner
    `src/leanfaith/sft1/sprint/square.py`): for every certified N25 negative `P ≁ C` the matching
    symmetry `T` (P18 for `eq_to_ne`, `P_NE_SYMMETRIZE_V1` for `ne_to_eq`) is applied to both
    endpoints; the typed diamond `T(N(P)) == N(T(P))` must hold as closed `Expr` and as rendered
    goal; certificates `P ↔ P′`, `P′ ↔ P`, `C ↔ C′`, the loaded source proof, the N25 refutation of
    `C`, the transported proof of `P′` (`Iff.mp`), the refutation of `C′` (`Iff.mpr`),
    `¬(C ↔ P)` and `¬(P′ ↔ C′)` are each Meta-checked and kernel-checked (`Kernel.check` /
    `Kernel.isDefEq` at level-zero instantiation); alpha-hash self-pair rejection over the four
    endpoints; rows are exactly `{reference, candidate, label}` with four rows per root in one
    ancestry group and one shard (`P′⇢P` pos, `C⇢C′` pos, `C⇢P` neg, `P′⇢C′` neg), so reference
    and candidate marginals are identical across labels by construction. P14/P23 recipes were not
    used (no typed diamond implemented for them).
  - Eligible pool: `targets/square_n25.json`, 1,587 certified N25 roots deduplicated by reference
    expr hash across `tenk`, `v2_ne`, `v2_lt` (roots sha `62f1f135…`).
  - Gates (chain logs `logs/square_gate_chain.log`, `logs/square_full_chain.log`): fixtures —
    retained `Nat.mul_factorial_pred`, fail-closed `PNat.gcd_comm` (`no_ground_assignment`),
    `Nat.factorial_lt` (`final_target_eq_ne_not_applicable`); fixture runs now bypass the semantic
    cache and wipe their run dir (live rerun: 2 Lean requests, 11.9 s Lean, 8.2 GB peak). 20-root
    run `square_20`: 80 rows, every row inspected (`runs/square_20/inspection/sample.md`, no
    defect), zero-Lean replay, zero duplicates. 100-root gate `square_100`: 400 rows, 13/13 checks,
    candidate-only 0.50 (UB 0.50), reference-only 0.50 (UB 0.50), family-held-out 0.50 (UB 0.515)
    (`compacted/core_v3_square_gate100`, local only).
  - Full run `square_full` (tmux `leanfaith-sft1-square-full`, one persistent worker, 24 GiB
    claim): 1,587 roots considered, 1,586 retained, 1 rejected (`Nat.cast_withBot`,
    `square_endpoints_not_pairwise_distinct`), 6,344 rows; 179 Lean requests, 390 s Lean,
    431 s wall, 60 batches, peak process-tree RSS 8.9 GB; 101 roots served from the square cache
    (gate runs), 1,486 via Lean; replay: 0 Lean requests, 0 duplicate rows.
  - Build: the first build failed its own gate (row-level global dedup left 68 partial squares;
    kept locally as `compacted/core_v3_square.failed_row_dedup_20260902T0331Z`, unpublished).
    Fixed by square-level selection (`select_squares`, commit `6ed5432`): squares are accepted or
    dropped whole in stable salted-hash order; 37 duplicate squares dropped
    (`duplicate_squares.json`; Mathlib aliases such as `WithBot.coe_ne_bot`/`WithBot.bot_ne_coe`
    and type-distinct statements that render identically such as `ENat.zero_ne_top`/
    `ENNReal.top_ne_zero`, both `⊢ 0 ≠ ⊤`); conservation 6,344 = 6,196 kept + 148 duplicate-square
    rows + 0 degenerate rows.
  - Release `core_v3_square`: 6,196 rows (3,098 positive / 3,098 negative), 1,549 roots
    (families `square_eq` 5,348 rows, `square_ne` 848 rows), 7 shards of ≤1,000 rows with roots
    never split (1:1000:74274691d50e, 2:1000:3ea5935f04b4, 3:1000:56ce87e64cab, 4:1000:3bbe4a92da4d, 5:1000:558df4bfa82e, 6:1000:a617cb434fab, 7:196:71def0a790be); 14/14 release checks;
    serialized-shard screens candidate-only 0.50 (UB 0.50), reference-only 0.50 (UB 0.50),
    family-held-out 0.4545 (UB 0.4621); per family (eq/ne) candidate-only 0.50/0.50,
    reference-only 0.50/0.50; label-permutation control max UB 0.5234 (seeds 1, 2); integrity
    validator 6,196/6,196 rows, 7 shards, zero issues (`integrity_report.json`); provenance one
    segment (engine `34e2f611…`, compile context `ctx:6ba8e3a3…`, implementation commit
    `4a10a289…` kept reachable as tag `sft1-square-full-run-4a10a28`; it was amended into
    `337cdaf` with a test-only difference after the run).
  - Publication: private Hub dataset `Lemmy00/leanfaith-sft1-deterministic-v1`, new immutable
    prefix `sprint_v1/core_v3_square`, revision `980816004294e060ac7561410f67a9ccc379ea3a`
    (parent `315b7988…`), 25 files, fresh-download hash verification passed; card generated
    from the actual gate (`compacted/core_v3_square/README.md`, `publication_receipt.json`).
  - Residual limitations: all negatives derive from certified N25 pairs (one negative mechanism
    family; the design's leak immunity is the identical-marginal construction, not mechanism
    diversity); only Eq/Ne symmetry recipes; N32/N31 absent; goal text omits types when a
    statement has no binders, so a few type-distinct theorems collapse to one row set (dropped as
    duplicates rather than kept twice); replay remains journal/cache replay with proof checks at
    original generation; the 4,940 `tenk`-sourced rows inherit the 10K's root preselection bias.
  - Status `waiting_user`: `core_v3_square` is published as the corrected candidate release;
    SFT1 is not declared complete or scaled. Open decision: adopt `core_v3_square` (6,196 rows)
    as the training-facing SFT1 view, and whether to grow it with P14/P23 typed diamonds or
    N32-based squares.

- 2026-09-02 — corrective Lean-free release `core_v3_square_v2` (commits `e8b07f7`, `4a61868`;
  no Lean run, `core_v3_square` prefix/revision untouched). Built from the same retained
  evidence (`runs/square_full` journal terminals + cache records + stored raw responses):
  - `reference_truth`/`candidate_truth` now derive from the square endpoints (P, P′ proved;
    C, C′ refuted): `C ⇢ P` refuted reference / proved candidate, `P′ ⇢ C′` proved reference /
    refuted candidate; validator checks every row kind exactly.
  - Explicit square-root cache identity in every sidecar (`cache: {kind, schema 2, key, path}`)
    replaces the nominal operation key; provenance loads each referenced cache file and
    verifies root, engine, compile context, terminal status, request hashes, alpha hashes,
    and commit (6,196/6,196 verified, 0 inconsistent); absent or inconsistent records fail the
    build and the validator. Records written before the commit field existed (the 100 gate-run
    roots) resolve their commit from the generating run manifest and record that source.
  - Process alpha hashes reconciled against `rebuildSquares` hashes in the stored render
    responses for all four endpoints of every root (1,549/1,549 matched, 0 quarantined).
  - Card corrected (direct kernel-checked `Not (Iff reference candidate)`); now uploads
    `duplicate_squares.json`, `permutation_control.json`, `quarantined_roots.json`,
    `alpha_reconciliation.json`, `rows_identity.json` (30 files).
  - Runner hardening for future runs: resume validates the run manifest (config hash, engine
    source/semantic version/import fingerprint, root-list hash, root count, max_roots);
    per-root `square_begin` → rows → `square_terminal` transaction with recovery on resume;
    readers take terminals as the authority; explicit cacheable-status whitelist
    (`ok`, `retained`, `rejected`, `not_applicable`).
  - Result: 6,196 rows / 1,549 roots, `rows.jsonl` byte-identical per shard to `core_v3_square`
    (`rows_identity.json`), 18/18 checks, unchanged screens (0.50 / 0.50 / 0.4545), integrity
    6,196 rows zero issues, three truthful provenance segments (commits `c2e6d086` 392 rows,
    `4a10a289` 5,800 rows, `337cdaf0` 4 rows).
  - Published privately: `sprint_v1/core_v3_square_v2`, revision
    `65f7a9192a45d43d663cf7393ee761d0f5ded78a` (parent `9808160042…`), fresh-download verified.
    `sprint_v1/core_v3_square` is marked superseded here and in `compacted/index.json` only.
  - Adopted `core_v3_square_v2` as the training-facing high-confidence SFT1 seed. Status
    `pilot_passed` (not complete). Next: time-boxed P14/P23 diversity expansion
    (`core_v4_diverse_square`), then `aux_n19_square_curriculum`, then return effort to SFT2.

- 2026-09-02 — diversity expansion `core_v4_diverse_square` (time-boxed; commit `29357db`).
  Engine: `SquareOp {id, neg, transforms}`; `SQUARE_N25_BINDER_V1` and `SQUARE_N32_BINDER_V1`
  apply the first applicable binder transform (P14, then P23) to `P` and replay it at the same
  site on `C`, with the same typed diamond, alpha self-pair check, and nine direct Meta/kernel
  certificates as the symmetry squares; N32 squares refute through `lt_asymm` grounding.
  - Fixtures (live engine, cache bypassed): symmetry op unchanged; N25-binder retained
    `Nat.gcd_fib_add_self` (P14), `Nat.choose_eq_choose_pred_add` (P23), `ZMod.prime_ne_zero`
    (P14 with `Fact` binders), fail-closed `PNat.gcd_comm`, `Nat.factorial_succ`
    (`square_no_applicable_transform`); N32-binder retained `Nat.ascFactorial_pos` (P14),
    `Nat.add_pred_div_lt` (P23), fail-closed `Nat.succ_pos'`, `Nat.factorial_succ`.
  - 20-root runs (6 + 9 squares, all 60 rows inspected: binder order swapped, hypotheses packed
    into one conjunction with a fresh name, strict order flipped; no defect), 100-root gate
    (`core_v4_diverse_square_gate100`: 296 rows / 74 squares, all checks, screens
    0.50 / 0.50 / 0.43); zero-Lean replays with zero duplicates throughout.
  - Full runs: N25-binder over the 1,587 N25 roots → 504 squares (1,083 `not_applicable`, zero
    rejections/errors; 120 Lean requests, 179 s Lean, 202 s wall); N32-binder over the 150
    certified N32 roots (`targets/square_n32.json`, 135 Nat / 15 Int) → 54 squares
    (96 `not_applicable`; 4 requests, 15 s Lean).
  - Release `core_v4_diverse_square`: 2,232 rows (1,116 / 1,116), 558 squares / 558 roots,
    families `square_n25_p14` 1,500, `square_n25_p23` 516, `square_n32_p14` 96,
    `square_n32_p23` 120; 3 shards; 18/18 checks; screens candidate-only 0.50 (UB 0.50),
    reference-only 0.50 (UB 0.50), family-held-out 0.5125 (UB 0.526; per family 0.54 / 0.47 /
    0.50 / 0.375), permutation-control max UB 0.539; integrity 2,232/2,232 rows zero issues;
    2,232 cache records verified; alpha reconciled for every row; provenance segments
    `29357db` (2,212 rows) and `8248f6f` (20 rows generated by the fixture runs before the
    engine commit, same engine source). Published privately at `sprint_v1/core_v4_diverse_square`,
    revision `2020c6d0aa10639119847c48667a8fd2b88ae80b` (parent `65f7a919…`), fresh-download
    verified; indexed in `compacted/index.json` as an additive diverse release.
  - Limitations: only 558 of 1,737 eligible roots admit a binder transform; N32 squares are few
    (54); no new N32 discovery, N31 grounding, or unconstrained negative mutations were used.

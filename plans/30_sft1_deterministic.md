# SFT1 — deterministic theorem-equivalence data at scale

> **Task ID:** SFT1
> **Status:** waiting_user
> **Owner/session:** Codex `/root` — 2026-08-30 SFT1 revision-0.3.0 session
> **Last updated:** 2026-08-30
> **Active proposal:** revision 0.3.0
> **Approval recorded:** pending; revision 0.2.0 was not approved
> **Dependencies:** a new coherent REPR freeze exposing
> `LeanFaith.GoalV1.renderClosedProp (e : Expr) : MetaM String`, including replacement spec/source
> hashes, a canonical universe profile, and passed real-goal coverage; additive shared-contract
> clarification; explicit user approval of revision 0.3.0
> **REPR predecessor:** `cbc933c3623d81ba649a1f9c5107ad404389d69f` was reviewed but is
> superseded and not consumable by SFT1
> **Next gate:** user approves or edits revision 0.3.0; task-owned implementation and every Lean
> gate remain blocked until the new coherent REPR freeze is pinned; 10K remains a later decision
> **Compute class:** zero Lean for the census; bounded persistent Lean Meta for retained gate rows
> only after approval and the renderer dependency
> **Lean budget:** no per-row process spawn; every retained gate row receives bounded typed Meta
> validation and evidence replay in persistent workers; no Lean is authorized in this revision turn
> **Local staging root:** `/storage/milikic/leanfaith/value_first/sft1_deterministic_v1/`
> **HF destination:** private `Lemmy00/leanfaith-sft1-deterministic-v1`

## Authorization boundary

Revision 0.3.0 is a design and invariant-test deliverable. The strict policy loader and its
non-Lean tests are authorized now. Transform implementation, Lean execution, one-example checks,
the approximately 100-root checks, the 10K pilot, row generation, scale, publication, and every
root/pair-count commitment remain unauthorized.

A user approval of the exact sentence at the end authorizes only task-owned implementation and the
one-example/approximately-100-eligible-root-per-operation gates, all conditional on first pinning
the new coherent REPR replacement freeze. Passing those gates permits a report and a new request
for the 10K pilot; it does not implicitly authorize that pilot.

## Objective and scale contract

Build a diverse SFT1 F1 corpus whose labels arise from exact row-local evidence plus admission of
the exact registered operation. Revision 0.3.0 makes no row-count commitment.

A 2–3M retained-pair range is a planning band to test against the zero-Lean census and later
measured pilots. It is neither a minimum, a promise, nor permission to generate rows. The arithmetic
is explicit: 500,000 eligible roots under an eight-pair cap can retain at most 4,000,000 pairs;
5,000,000 pairs would require at least 625,000 eligible roots at that cap. Any root ceiling, pair
target, 10K pilot, bulk run, stretch target, or publication requires a later explicit decision.
The older plan shorthand “5M pairs” names only that unapproved stretch scenario; it is not an
active target or frozen row commitment in revision 0.3.0.

Before any row commitment, perform a zero-Lean census over pinned source metadata/files and publish
a source-eligibility matrix. It must report raw theorem/lemma counts, license/revision eligibility,
exact and near-duplicate clusters, source/domain/signature strata, exact import-context availability,
and the expected closed-Expr construction route. No target may be manufactured by multiplying a
root guess by a quota.

## Representation and source-to-Expr contract

The previously reviewed REPR freeze at
`cbc933c3623d81ba649a1f9c5107ad404389d69f` (spec
`073d92c8e1fcc5cb7a3a9bf325d047e9b2d52149504977086de46abf6f84ef52`) is recorded only as
reviewed-but-superseded evidence. SFT1 does not consume it, verify it as a live dependency, or pin
its config/Lean/Python hashes as execution requirements.

SFT1 remains hard-blocked until REPR publishes a **new coherent freeze** exposing the exact API
`LeanFaith.GoalV1.renderClosedProp (e : Expr) : MetaM String`. The replacement commit, spec hash,
config hash, Lean source hash, Python source hash, canonical-universe-profile ID/hash, and real-goal
coverage receipt are currently null/pending in the policy.

Reference and candidate must each be a canonical closed proposition `Expr`. In one persistent Meta
request, SFT1 calls the exact shared `renderClosedProp` function directly on the reference Expr and
then the candidate Expr. It may not copy the renderer or options, surface-render the candidate,
pretty-print and re-elaborate it, declare a candidate theorem/axiom, synthesize a candidate proof,
use `sorry`, or compile `goal_v1` text.

The shared API must reject expression/universe metavariables, free variables, loose bound
variables, ill-typed expressions, non-`Prop` expressions, and any anonymous binder that would be
exposed as a named local in the rendered outer Pi telescope. Structural anonymous Pis rendered as
arrow syntax are not named-telescope residue. Both Exprs use the exact canonical universe profile
supplied by the replacement REPR freeze; the current engine's local `u_i` convention is forbidden
unless it is exactly that shared profile.

The replacement also defines a `renderer_api_hash` as the canonical hash of exactly its replacement
commit, Lean renderer path/hash, namespace, and signature. The universe profile and render context
remain separate hash axes rather than being folded into that code-identity hash.

The two source routes are separate:

- **Imported constants (Mathlib, Physlib, CSLib):** look up `ConstantInfo`, take its complete type,
  instantiate its universe parameters with the canonical recorded universe profile, then require
  closure, successful type inference, and `Prop`. The resulting type `Expr` is the reference.
- **`compiler_data` signatures:** the zero-Lean census first verifies pinned signature text and
  exact compile context. Later, one persistent `TermElabM` worker elaborates the complete binder
  telescope and result directly into a proposition `Expr`, instantiates metavariables once, and
  rejects unresolved/open/non-`Prop` results. It never inserts a theorem declaration. A signature
  lacking a reproducible context is source-ineligible.

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
and the final shared-API rendering, proving that no anonymous binder and no `[anonymous]` substring
reaches `goal_v1`.

## Label contract

### Positive operations

A positive row needs exact `P-DEF`, `P-SCHEMA`, `P-LEMMA`, or `P-REFLECT` evidence for the
emitted closed pair plus explicit admission of its exact operation ID. Family polarity or endpoint
provability cannot create a label.

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
  fixtures, and user admission. This lane says that the candidate changes the admitted protected
  claim dimension; it makes no claim about F2 direction or candidate truth.
- **N-PROOF:** a stronger, capped subtype of an admitted N-RUBRIC operation. It retains and replays
  an exact source proof and an exact candidate refutation for the same closed Expr pair under an
  allowed axiom profile. Aggregate N-PROOF share is capped at 10%, with stricter per-operation caps.

Generic `D0`, failed proof search, F2 direction, and abstract separators are non-label evidence.
Every retained sidecar records candidate truth evidence as exactly `proved`, `refuted`, or
`unknown`; that field is separate from the F1 label and cannot select a lane or create a label.

All evidence closes the complete telescope and binds universes, ordered locals, binder kinds, local
definitions, implicit/instance binders, coercions, hypotheses, target, transparency, logic regime,
dependencies, and allowed axiom profile.

## Exact operation registry and frozen banks

The authoritative registry is
[`proposed_composition_policy.yaml`](../configs/transformations/sft1_value_first_v1/proposed_composition_policy.yaml).
Family prose is descriptive and cannot authorize an umbrella family.

Every one of its 46 exact operations records: operation ID; family, track, status, and lane;
mechanism superclass; schema/lemma/procedure anchor hash; orientation; typed applicability; context
restrictions; transparency; logic regime; allowed axiom profile; inverse token; exact cap; heartbeat,
soft, and hard time budgets; eligible projects; success and adversarial-rejection fixtures;
claim-erasure guards where applicable; candidate-truth default; exact-delta requirement; and an
operation-local admission object. All admissions, executability flags, and label-emission flags are
false in the checked-in proposal.

Nine exact family-and-rubric-dimension admission records bind every N-RUBRIC operation and its
N-PROOF sibling, including the two separate N28 synthetic dimensions. They are independently
pending and false; neither an operation admission nor a family disposition can substitute for this
family/dimension decision.

The design-frozen
[`starter_banks_v0_3_0.yaml`](../configs/transformations/sft1_value_first_v1/starter_banks_v0_3_0.yaml)
contains hash-bound starter entries for P20, P32, P34, P35, P39, P41, P42, the N-RUBRIC protected
dimensions, N-PROOF templates, and separate N28 synthetic templates. Every entry is either bound to
exact operation IDs or explicitly `reserved_unadmitted`. Its Lean-resolved anchor hashes remain
null, which is intentional: they must be resolved and pinned after approval but before an operation
can execute.

The exact registry dispositions are:

- P02 and P11 are diagnostic and cannot emit rows.
- P01 has one narrow exception allowing its single hop to repeat an alpha fingerprint once; it may
  not repeat a text, closed-Expr, render, operation, or inverse token.
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

`P` is an admitted positive exact operation; `N` is one admitted N-RUBRIC operation or its
capped N-PROOF subtype. A positive row has zero negative operations; a negative row has exactly one,
last and unique. Each hop rediscovers a unique typed site in the current Expr. Sites are pairwise
disjoint, mechanism superclasses do not repeat, inverse tokens do not repeat, and
text/Expr/render/site-lineage cycles reject the chain. The sole P01 exception is the one
alpha-fingerprint condition stated above.

The seven exact P20 fold/unfold, P21 beta/zeta introduction/reduction, and P22 eta-reduction
operations form one explicit mutual-exclusion group: at most one of them may appear in a chain.
This makes the single-definitional-mechanism rule executable rather than relying on family prose.

The deterministic retention order is: source eligibility; typed applicability; root blocklist;
pre-validation candidate sampling; typed Meta validation/replay; post-transform blocklist;
stable row-hash total ordering; canonical-unordered-pair duplicate/conflict classification; retain
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

## Lean is the bottleneck: execution contract after approval

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

Before the one-example gate can start, all of the following must be true:

- the new coherent REPR replacement freeze and exact API/source/spec/universe hashes are pinned;
- the additive shared-contract rule for exact evidence plus family/dimension and operation admission
  has been merged and pinned;
- reference and candidate closed Exprs render successfully by direct calls to the shared API in the
  same persistent Meta request;
- a pair required to be distinct has distinct output, and each rendering contains exactly one
  turnstile;
- neither Expr nor rendering contains expression/universe metavariables, free variables, loose
  bvars, or anonymous names exposed as rendered outer-telescope locals, and type inference succeeds
  before either render;
- P23's no-anonymous-binder regression passes;
- REPR's replacement-freeze real-goal coverage regression passes;
- failures are counted by source, family, operation, polarity, and exact failure class; and
- stable IDs and sidecars bind reference/candidate closed-Expr and rendered-output hashes, the
  replacement commit, renderer/spec hashes, canonical-universe-profile ID/hash, and render-context
  ID/hash.

The exact failure taxonomy includes reference/candidate render failure, Expr or universe mvar,
free variable, loose bvar, anonymous binder, ill-typed/non-Prop Expr, wrong turnstile count,
required-distinct collapse, universe-profile mismatch, render-context mismatch, and missing REPR
coverage evidence.

## Gates

### Zero-Lean census

Before any row commitment or one-example gate, complete the census and source-eligibility matrix
without invoking Lean. Census absence cannot be filled by a guessed target.

### One-example gate

For every exact operation and each explicitly eligible project, retain one successful example and
one adversarial rejection with the expected rejection reason. Zero yield is not silently waived; a
census-backed incompatibility requires an explicit policy revision. Replay 100% of retained
certificates in the persistent Meta backend. This gate requires user approval plus the new coherent
REPR freeze, all representation pre-gate checks, P23 regression, and resolved inline-anchor/fixture
freezes.

### Approximately 100 eligible roots per operation

Only after the corresponding one-example matrix passes, evaluate approximately 100 eligible roots
per exact operation, again with 100% retained-row typed validation and replay. Report applicability,
pre-validation sampling, successes/rejections, failure classes, cache behavior, Lean-seconds,
sidecar bytes, RSS, source/polarity yield, and deterministic resume/replay.

### Later 10K decision

After the 100-root report, stop and request a separate user decision for the 10K pilot. A future
10K pilot must satisfy all of the following, with 95% stratified-cluster-bootstrap confidence
bounds whose upper bounds remain below the thresholds:

- candidate-only and reference-only balanced accuracy each strictly below 0.60;
- paired family-, mechanism-, and template-held-out balanced accuracy each strictly below 0.65;
- deterministic 50% orientation swap in training only, after caps and the orientation-invariant
  duplicate/conflict screen but before the final model-facing assertion;
- intact root-ancestry and near-duplicate clusters across splits;
- root-level and post-transform evaluation-blocklist screens;
- global model-facing duplicate and conflicting-label rejection;
- joint source×polarity stratification without forced rows; and
- 100% retained-certificate replay in persistent Meta.

The former `0.70` shortcut allowance is superseded: revision 0.3.0 uses only the stricter
candidate/reference and paired held-out balanced-accuracy thresholds above, each with its required
confidence bound.

The 10K pilot, any bulk scale, publication, and all row/root-count commitments are unauthorized.

## Acceptance criteria for this revision

- Revision 0.2.0 remains unapproved; revision 0.3.0 records no user approval.
- The exact 46-operation registry and 30-entry frozen design bank load through a strict typed loader,
  reject unknown/duplicate/drifting fields and hashes, and cannot execute or emit labels.
- The reviewed `cbc933…` predecessor is explicitly non-consumable; replacement REPR commit/spec,
  Lean/Python, universe-profile, and coverage fields remain null and block implementation/Lean.
- Negative labels use N-RUBRIC or its capped N-PROOF subtype; D0 and candidate provability do not
  create labels.
- Every P-LEMMA/P-REFLECT entry carries all claim-erasure guards.
- The zero-Lean census, source routes, exact grammar, axiom profiles, cache keys, deterministic cap
  order, source/polarity balancing, scale arithmetic, and gates are invariant-tested.
- No Lean process, transform implementation, row generation, staging, publication, or frozen/shared
  artifact mutation occurs in producing revision 0.3.0.

## Writable paths and ownership

**Writable SFT1 areas after the applicable gate:** this brief; `src/leanfaith/sft1/`;
`LeanFaith/Meta/SFT1/`; `configs/transformations/sft1_value_first_v1/`;
`tests/unit/sft1/`; named SFT1 live fixtures; and the SFT1 staging root.

Shared plans, REPR, existing transform/Meta engines, project/dependency config, historical outputs,
frozen manifests, and user work are read-only. Changes there require a coordinator request.

**Exact paths claimed by this session:**

- `plans/30_sft1_deterministic.md`
- `configs/transformations/sft1_value_first_v1/PROPOSED_TRANSFORM_AUDIT.md`
- `configs/transformations/sft1_value_first_v1/proposed_composition_policy.yaml`
- `configs/transformations/sft1_value_first_v1/starter_banks_v0_3_0.yaml`
- `src/leanfaith/sft1/__init__.py`
- `src/leanfaith/sft1/composition_policy.py`
- `tests/unit/sft1/__init__.py`
- `tests/unit/sft1/test_composition_policy.py`

The package/test paths are claimed only for a design-only loader and non-Lean invariant tests. No
Lean module, transform implementation, shared contract, REPR file, data path, or frozen track is
claimed.

## Coordinator requests

1. **REPR coordinator:** supersede rather than extend SFT1's reviewed `cbc933…` predecessor with a
   new coherent freeze exposing the exact committed API
   `LeanFaith.GoalV1.renderClosedProp (e : Expr) : MetaM String`. Return the replacement commit,
   spec/config hash, Lean source path/hash, Python source path/hash, canonical-universe-profile
   ID/hash and naming contract, canonical renderer-API hash and exact hash basis, render-context
   contract, and a passed hash-bound real-goal coverage regression. The API must reject open,
   metavariable-bearing, ill-typed, and non-`Prop` Exprs plus anonymous binders exposed as named
   outer-telescope locals. Add a regression proving reference and transformed candidate can call
   this exact function directly in the same persistent Meta request. SFT1 will not copy rendering
   code or options, declare endpoints, synthesize proofs, surface-render/re-elaborate candidates,
   use `sorry`, or compile `goal_v1` text.
2. **Shared-contract coordinator:** update `plans/00_shared_contracts.md` additively so an SFT1
   binary label requires exact row-local evidence plus admission of the exact registered operation
   and its family/rubric dimension. Polarity multiplication, generic D0, F2 direction, failed
   search, and candidate provability alone never create a label. Record N-RUBRIC and capped N-PROOF
   lanes and keep candidate truth evidence `proved|refuted|unknown` separate from the label.

Neither request authorizes this session to edit the coordinator-owned path.

## REPR dependency checklist before one-example Lean

- new coherent replacement commit and frozen spec/config hashes;
- Lean module path/hash exposing the exact `renderClosedProp` signature;
- Python integration source path/hash for the same freeze;
- canonical-universe-profile ID, hash, and deterministic naming contract shared across
  REPR/SFT1/EVAL/SFT2;
- render-context/options identity and a direct same-persistent-request reference/candidate test;
- canonical `renderer_api_hash` plus its exact payload/hash basis;
- hash-bound real-goal coverage regression ID/hash with passed status; and
- SFT1 pins all of the above, then passes its P23 no-anonymous-binder and representation pre-gate
  regressions.

Independent label-contract prerequisite: the coordinator merges and pins the additive shared rule
that exact evidence plus the required family/dimension and exact-operation admissions—not polarity
multiplication—creates an SFT1 label.

## Exact user decision required

> Approve SFT1 proposal revision 0.3.0 solely for task-owned implementation and the one-success plus one-adversarial-rejection per exact operation and eligible project gate followed by the approximately 100 eligible roots per operation gate, all to begin only after SFT1 pins a new coherent REPR freeze exposing `LeanFaith.GoalV1.renderClosedProp (e : Expr) : MetaM String` with its spec, Lean/Python, canonical renderer-API, canonical-universe-profile, render-context, and passed real-goal-coverage hashes and the coordinator merges the additive shared SFT1 label rule; the reviewed `cbc933c3623d81ba649a1f9c5107ad404389d69f` predecessor is not consumable, and the 10K pilot, bulk generation or scale, publication, and every production root or pair-count commitment beyond those two gates remain unapproved.

## Session kickoff prompt

```text
Own only SFT1 in /localhome/milikic/LeanFaith. Read AGENTS.md, PLAN.md,
plans/00_shared_contracts.md, plans/30_sft1_deterministic.md, and TRANSFORM_CATALOG_V2.md. Claim
exact paths before edits. Audit and propose preserving/breaking operations with exact applicability,
composition safety, value, and Lean cost. Do not generate rows until explicitly approved. Never
spawn a process per row: pre-sample cheaply, then validate and replay every retained row in bounded
persistent Meta workers with durable journals and exact caches. Preserve frozen tracks and user
work. End with the exact user decision needed.
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

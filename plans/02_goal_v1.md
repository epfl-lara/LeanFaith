# REPR — shared `goal_v1.0` theorem representation

> **Task ID:** REPR
> **Status:** active
> **Owner/session:** Codex REPR session (2026-08-30)
> **Last updated:** 2026-08-30 (fourth repair: shared closed-Expr route and validator coverage)
> **Dependencies:** none
> **Next gate:** finish Lean-free API/schema/859-row validator work, run one bounded shared-session
> live gate, then commit an active implementation and one coherent replacement freeze
> **Compute class:** CPU; bounded Lean oracle/renderer tests only
> **Lean budget:** reuse one loaded environment and already-certified Exprs; no corpus-wide compile
> **Local staging root:** `/storage/milikic/leanfaith/value_first/goal_v1_0/`
> **HF destination:** none; the serializer version is embedded in downstream manifests

## Objective

Own the one canonical model-facing theorem representation so SFT1, SFT2A, SFT2B, and EVAL do not
build incompatible renderers. Freeze `goal_v1.0`, its provenance flag, and the separate raw
compilation context. This is an enabling task, not a corpus-generation task.

## Frozen representation decision

`goal_v1.0` contains ordered local variables, hypotheses, typeclass/universe locals, and exactly
one `⊢ target`. It removes declaration name/kind, attributes, command shell, imports, options,
comments, the declaration's proof delimiter/body (`:=`, `by`, `sorry`), and proof-only text. Term
bindings and the bounded parenthesized named-argument form retain their meaningful `:=`. Preserve local names/order, dependent types,
generated instance names, coercions, universes, and meaningful line boundaries. Preserve notation
except for the explicit term-binding normalization below: supported surface `let`/`have` and Lean's
elaborated `have` presentation all serialize with the canonical keyword `let`; the raw sidecar keeps
the original spelling.

Use the best cheap source available, with one shared Lean text implementation:

1. `closed_prop_expr`: SFT1 supplies the already-certified reference and candidate `Expr`s while
   both remain live in one `MetaM` request. `LeanFaith.GoalV1.renderClosedProp` renders both directly;
   this route creates no theorem/axiom, proof, or `sorry`, never calls surface mode, and never
   pretty-prints then re-elaborates a candidate.
2. `elaborated`: render a named theorem's `ConstantInfo.type` from an environment that is already
   loaded or from candidate compilation already required elsewhere. `renderConstantType` delegates
   literally to `renderClosedProp ci.type`; it has no second renderer and does not recompile proofs.
3. `surface`: deterministic Lean-aware extraction from a trusted headless/signature string when
   elaboration is unavailable or would trigger bulk compilation.

All three yield the same textual grammar and store
`goal_v1_source: closed_prop_expr|elaborated|surface`. Never silently mix them without the sidecar
flag; report coverage and model metrics by source mode. Ambiguous surface rows fail closed or retain
the raw representation outside the core view. Every route uses the frozen first-occurrence universe
profile `u_0`, `u_1`, ...; supported surface signatures canonicalize explicit simple `Type`/`Sort`
level names and fail closed on inferred, star, or compound surface level syntax.

`goal_v1` is model-facing, not a compilable source language. Declaration-backed routes retain the
exact nonempty `raw_statement`. A direct Expr with no declaration retains either the exact original
`proposition_text` or the explicit `constructed_expr_no_source_text` absence reason; missing text is
never filled from rendered goal text. Every sidecar retains `project_id`, project/toolchain revision,
import header, namespaces/scopes/options in `compile_context`, renderer/spec hashes, implementation
hashes, and route-specific provenance. SFT2 proposers/formalizers must return a compilable
declaration/signature; compilation never tries to reconstruct source from goal text. This avoids an
under-specified inverse transformation.

Do not alpha-normalize model text. A typed/alpha-normalized fingerprint may be a separate dedup key.
Filter theorem/lemma declarations before serialization because declaration kind is absent afterward.

## Scope and ownership

**In scope:** versioned spec/examples, surface parser/serializer, the sole Lean-side closed-Prop
renderer for named and direct Expr routes, provenance and compile-context schema,
normalization/fingerprint hooks, fixtures/tests, and a small cross-source oracle.

**Out of scope:** bulk corpus rendering, semantic labels, theorem transformations, proof
compilation, LLM calls, replacing raw source fields, generating training data, or supporting `def`
declarations as named v1.0 inputs.

**Writable paths:** this brief; `src/leanfaith/representations/goal_v1.py`;
`LeanFaith/Meta/GoalV1.lean`; `configs/representations/goal_v1_v1.yaml`;
`tests/unit/representations/` and `tests/integration/leaninteract/test_goal_v1_live.py`. Existing
representation/backend modules are shared/read-only; request coordinator changes if integration
cannot be achieved additively.

## Input and output contract

Surface and named elaborated routes require declaration kind, exact nonempty `raw_statement`, and
`CompileContext`. Surface additionally requires the caller-attested `parsed_signature`; elaborated
mode requires a declaration name or loaded-constant lookup. Their serialized wrapper is
`{record, raw_statement, compile_context}`. Its record contains:

```text
representation_id, goal_v1, goal_v1_source, renderer_version, spec_hash,
raw_statement_hash, declaration_kind, compile_context_id,
implementation_identity, typed_alpha_fingerprint?, warnings[]
```

The additive direct-Expr wrapper is `{record, source_material, compile_context}`. The reference and
all candidates are emitted atomically from one `closed_expr_in_session` request. Its record binds:

```text
representation_id, goal_v1, goal_v1_source=closed_prop_expr, renderer_version, spec_hash,
compile_context_id, endpoint_id, endpoint_role, source_material_hash, rendered_goal_hash,
provenance, implementation_identity, typed_alpha_fingerprint?, warnings[]
```

`provenance` contains the SHA-256 of the validated canonical structural Expr tree, original and
canonical used level parameters, universe-profile ID/hash, render-scope ID, render-context ID/hash,
route ID, and Expr origin. `implementation_identity` contains the semantic renderer API hash plus
the exact checked-in Lean, injected-helper, Python, and config hashes and their aggregate hash.

Downstream core rows include only the `goal_v1` text in `reference`/`candidate`; downstream
manifests and keyed sidecars retain the full record, source-material union, compilation context,
implementation identity, and Expr hashes.

## Lean-efficiency plan

Lean is the bottleneck. Build/test parsing, schemas, payload validation, hashing, and the 859-row
post-validator fixture without Lean. For live fixtures, load one pinned project once and render
`ConstantInfo.type` plus live certified Expr values from the existing environment; never recompile
theorem proofs or run one Lean process per theorem/candidate. SFT1 invokes the shared API in its
existing Meta request; SFT2 reuses it during compilation already required for each candidate. Cache
by structural Expr/raw hash plus project/toolchain/options, render-context hash, and renderer
implementation hashes.

## Repair plan

1. Finish all safe string parsing, schema validation, source filtering, hashing, and fixtures before
   invoking Lean.
2. Freeze the tagged surface fallback and sidecar/compile-context contract with unit tests, including
   theorem/lemma filtering and fail-closed ambiguity handling.
3. Add one elaborated renderer behind the existing central Lean protocol boundary, reuse one loaded
   environment for bounded fixtures, and never invoke one Lean process per declaration.
4. Run one complete cross-path example first, then only the bounded multi-source pilot; record
   agreement, failures, cache behavior, throughput, leakage checks, hashes, risks, and handoff.
5. Fail the whole elaborated batch when Lean reports any sorry and `allow_sorry=False`, including
   mixed `INVALID` batches; retain partial payload-backed successes only when no sorry was reported.
6. Use one structural analyzer on extraction, surface rendering, elaborated rendering, and final
   validation of trusted signatures. Canonicalize complete semicolon-delimited term `let`/`have`
   chains at any balanced delimiter depth to `let`; fail closed on incomplete chains, layout-only or
   macro bindings, and ambiguous same-context values. Never infer a signature or proof boundary from
   raw declarations under potentially loaded syntax.
7. Expose `renderClosedProp (e : Expr) : MetaM String`, instantiate assigned metavariables, and fail
   closed on unresolved expression/universe metavariables, free or loose variables, malformed or
   non-Prop expressions, `sorry`, and anonymous outer telescope binders. Make metadata-transparent
   anonymous-binder inspection explicit.
8. Validate every direct payload's exact JSON schema and closed structural Expr tree before hashing;
   reject duplicate/spoof-shaped endpoints atomically. Pin both the full Lean helper and exact
   import-stripped injected body at runtime.
9. Keep the 859-row ConsistencyCheck projection as a derived goal-only test fixture, add narrow bar,
   postfix, set-image, big-operator, delimiter, generated-name, and structure rules, and retain
   fail-closed malformed near-neighbor tests.

Lean is the bottleneck: this session will not compile a corpus. It will establish the small Lean
oracle, measure the cheap renderer against it, and compile only the bounded audit required here.

## Execution gates

### One-example smoke

Render the same simple theorem through elaborated and surface paths, compare the frozen text,
serialize its compile context, and prove no declaration shell/proof leaks. Show downstream code can
join the core text to raw compilable source without attempting a goal-to-source inverse.

### Pilot

Use a bounded suite spanning Mathlib, Physlib, CSLib, compiler-style source, canonical gold, and
ConsistencyCheck examples. Include dependent/shadowed binders, generated instances, universes,
coercions, multiline goals, helper declarations, comments/strings, and final-`def` rejection.
Report exact elaborated/surface agreement, fallback/failure rates, deterministic hashes, cache
behavior, throughput, and raw-text leakage.

### Freeze

Publish the spec/config/fixtures and version hash in the repository. Downstream task manifests pin
that hash. Later representational experiments create `goal_v1.1` or a new view; they do not mutate
v1.0.

## Acceptance criteria

- One owner and one `renderClosedProp` text implementation serve named constants, direct SFT1 Exprs,
  and every SFT/evaluation task.
- The output has ordered locals plus exactly one turnstile and no shell/name/proof leakage.
- Direct Expr, elaborated, and surface modes are explicit, measured, and never require mass Lean
  compilation.
- Exact raw/proposition source or an explicit no-text reason plus compile context is retained
  separately; no inverse from goal text is assumed.
- Tests cover difficult binders, environments, multiline output, determinism, and theorem-only
  filtering; direct-Expr tests cover the shared request, closedness failures, structural hashes, and
  no declaration/proof/sorry/text-round-trip behavior.

## Superseded candidate freeze record and evidence

The record below describes the rejected pre-review candidate and is retained as historical evidence.
It is not an active freeze or a downstream authorization. The replacement freeze record must use the
post-repair spec and committed code revision.

- **Frozen version:** `goal_v1.0`; renderer version `goal_v1.0`; canonical spec hash
  `690d57348e2098dd7aeda10eede6cec9182aa002eb69ec910ef3d47269ea160c`. The hash is
  SHA-256 over the canonical JSON form of the config's `spec` subtree and is asserted against the
  Python constant.
- **Canonical paths:** `src/leanfaith/representations/goal_v1.py` (forward API and tagged surface
  fallback), `LeanFaith/Meta/GoalV1.lean` (the sole elaborated renderer), and
  `configs/representations/goal_v1_v1.yaml` (frozen grammar, sidecar schema, smoke, and pinned pilot
  fixtures). No training rows or Hugging Face artifacts were created.
- **One-example cross-path smoke:** the same theorem rendered exactly as
  `x y : ℕ\nh : x < y\n⊢ x ≤ y` through both paths. Cold request was 1,424 ms; replay was 9 ms;
  both used request hash
  `810b6efe2509d601b899b903b5c4bf09d571e87cb00b790818efedf37a52d124`. Raw source and
  compile context remained joinable in both sidecars, with zero shell/name/proof leakage.
- **Bounded loaded-environment pilot:** six elaborated fixtures in one request covered universes,
  a generated instance name, dependent and shadowed binders, coercions, helper declarations,
  anonymous arrow premises, multiline goals, and a proof-body sentinel. All 6/6 rendered in 21 ms
  (about 286 rows/s after startup), with exactly one turnstile each and zero raw/proof leakage.
  Surface comparison had one exact agreement among two renderable rows, four deliberate fail-closed
  rows, and one notation/coercion spelling difference; mode-specific metrics therefore remain
  mandatory.
- **Bounded multi-source surface pilot:** six pinned fixtures spanned Mathlib, Physlib, CSLib, Lean
  compiler source, canonical gold, and ConsistencyCheck. Four rendered in 1 ms total; Mathlib and
  CSLib failed closed as specified because their cheap signatures contained anonymous instance
  binders. No source corpus was compiled.
- **Verification:** `ruff check` passed; strict `mypy` passed for the implementation and owned tests;
  `pytest tests/unit/representations/test_goal_v1.py -q` passed 14/14; and
  `pytest tests/integration/leaninteract/test_goal_v1_live.py -q -s` passed 1/1. The measured live
  verification used 1.83 s wall time and 122,972 KiB peak RSS. Lean ran with one reserved worker and
  synchronous elaboration; the reservation was released. Raw Lean responses used pytest's isolated
  temporary directory and were not promoted as release artifacts.

## Superseded first replacement freeze record and evidence

This record was superseded by the second correctness review. Its hashes and measurements remain
historical evidence only and are not authorized for downstream use.

- **Frozen identity:** `goal_v1.0`, renderer `goal_v1.0`, canonical spec hash
  `2fc5b69c0534449d4ffeca0f47fddec38042fff90de374b3bda81d4f25dd23d8`. The hash independently
  recomputes over canonical JSON for the YAML/Python-identical `spec` subtree. The reviewed
  implementation revision is `b871900e5177bbda38471f98cc46818bb6502b0d`; the config additionally
  pins the Lean renderer SHA-256
  `8a2626489d65c7424f039f673fe5910adeb07d833580f5a6870cbf8e19434809` and Python module SHA-256
  `a3ef37d6d10713abca85d61802841fd4c8026798b6ed8d0b77793eebe4c0ed90`.
- **Semantic and width regressions:** the elaborated renderer uses `forallTelescope`, not its
  reducing variant, and the final `Format.pretty` call has width 1,000,000. The live fixtures assert
  exact preservation of `p : Prop\nhp : p\n⊢ ¬¬p` and assert that a local line longer than 120
  characters remains one physical line.
- **Applied compilation context and loaded constants:** imports, raw preamble, sorted structured
  options, opens, scoped opens, and nested namespaces are emitted before inline candidates. The live
  structured-context theorem depends on the namespace/open/scope contract. `lookup_only=True`
  rendered the already-imported `lf_add_comm` without redeclaration while its exact compilable source
  and project/import/options context remained in the sidecar. Helper payloads report `ConstantInfo`
  kind, and Python accepts only theorem constants.
- **One-example cross-path smoke:** surface and elaborated paths agreed exactly on
  `x y : ℕ\nh : x < y\n⊢ x ≤ y`; cold and replay request hashes both equaled
  `147f755a4bcb9c998ea2aa5904957f654e528f97f003ad508e3a206d81c068eb`. Cold rendering was 1,437 ms
  and the cached replay 22 ms.
- **Bounded pilots:** one loaded environment rendered 10/10 elaborated fixtures in 41 ms, including
  reducible negation, long width, structured context, and imported lookup. Surface mode rendered six
  of those ten: four exact agreements, two spelling disagreements, and four deliberate fail-closed
  outcomes. The separate six-source surface pilot rendered 4/6 in 1 ms; Mathlib and CSLib remained
  the two expected anonymous-instance failures. The full owned live gate used 1.92 s wall time and
  118,740 KiB peak RSS. No corpus was compiled.
- **Pinned ConsistencyCheck qualification:** this is a derived bounded fixture, not a copied dataset
  goal. At revision `1c6a6cca0f87b48d4cccb49946d3b8fc57a1eef9`, the 859-row source file hashes to
  `81cf6d9988625d84efbd8e1d6a0af4c234b2206da8350ee1d8bf547e612b1d47`; the selected row's
  `formal_statement` hashes to `bf963db06cfe1d75498daba3defa86a95bf720d225b21f57f496b82c027c1ddc`.
  The fixture reflows that field and adds `sorry` after its existing trailing `:= by`; its expected
  text is LeanFaith's surface rendering, not the upstream `goal` field.
- **Verification and cleanup:** scoped ruff, formatting, strict mypy, source-hash assertions, 22
  owned unit tests, the one live integration gate, and the repository task-plan pre-commit hook pass.
  No training data, external model call, durable staging artifact, or active host reservation remains.

## Superseded second replacement freeze record and evidence

This record was rejected by the third correctness review. Its hashes, commits, and measurements are
historical evidence only and are not authorized for downstream use.

- **Frozen identity:** canonical spec hash
  `7ec7b82923b4eb78a737f47653dfc7d7b5eb619373159ec1cf5ed0d794759ae9`; unchanged Lean renderer
  SHA-256 `8a2626489d65c7424f039f673fe5910adeb07d833580f5a6870cbf8e19434809`; replacement Python module
  SHA-256 `d2c0f0121f7085468f442eeb32bc3deac067f2746cf081c983a3b5fb262d81e0`. The reviewed replacement
  implementation revision is `75923b3ca5aa3915c0fee1278f45b547d9c96e3c`.
- **Sorry regression:** the live mixed batch compiled one `by sorry` theorem and failed a second
  declaration. With `allow_sorry=False`, the API returned zero sidecars and two explicit failures.
  Any nonempty backend `sorries` payload now triggers the same fail-closed policy as
  `VALID_WITH_SORRY`; `allow_sorry=True` remains an explicit opt-in.
- **`let` regression:** surface extraction tests candidate proof delimiters in order, so the binding
  assignment is retained and the later theorem proof delimiter is removed. Lean's multiline
  `⊢ have x := 1;` goal layout is recognized as a top-level let binding and canonicalized to exactly
  `⊢ let x := 1; x = 1`; the surface and elaborated live results agree. Layout-only surface input and
  unsupported multiline elaborated shapes fail closed instead of being flattened speculatively.
- **Bounded gate:** the unchanged cross-path smoke retained request hash
  `147f755a4bcb9c998ea2aa5904957f654e528f97f003ad508e3a206d81c068eb` (1,571 ms cold, 22 ms replay).
  The loaded environment rendered 11/11 elaborated pilot rows in 43 ms; surface mode had five exact
  agreements, two spelling differences, and four deliberate failures. The six-source pilot remained
  4/6 in 1 ms. Full live wall time was 2.09 s with 116,696 KiB peak RSS. No corpus was compiled, no
  training data was generated, and the host reservation was released.
- **Verification:** 26/26 owned unit tests, the one-example/live pilot, scoped ruff and formatting,
  strict mypy, plan validation, config/source/spec hash assertions, and both replacement pre-commit
  runs passed. The active-state implementation and every REPR-owned path were committed before this
  freeze record was finalized.

## Superseded third replacement freeze record and evidence

The coherent freeze commit `cbc933c3623d81ba649a1f9c5107ad404389d69f` is mechanically valid
for named theorem constants and caller-attested surface signatures, but it has no supported direct
closed-`Expr` route for SFT1 and is superseded for all new downstream pinning. Its evidence remains
historical only.

The active implementation parent commit
`fc612cb0816e83a8b29625b67988d95e444a7eb2` is reproducible provenance but is not consumable alone
because its config and test deliberately say `active`. The coherent downstream revision is the
commit containing this record, the frozen config, matching frozen-state test, and completed brief.

- **Frozen identity:** canonical spec hash
  `073d92c8e1fcc5cb7a3a9bf325d047e9b2d52149504977086de46abf6f84ef52`; unchanged Lean renderer
  SHA-256 `8a2626489d65c7424f039f673fe5910adeb07d833580f5a6870cbf8e19434809`; replacement Python module
  SHA-256 `09912e28687756a9cd203e6069a9f9c130259840d8309a3126fe1c8e1f333cbb`; active implementation
  parent `fc612cb0816e83a8b29625b67988d95e444a7eb2`.
- **Structural binding repair:** one balanced-context analyzer now gates trusted signatures,
  elaborated payload canonicalization, and final validation. Every visible term
  `let`/`have` requires one simple name under the path-specific policy below, same-context `:=`, nonempty value,
  same-context `;`, and a structurally complete body. Sequential, nested, local-type, literal, and
  colon-only multiline-local cases are validated independently; ambiguous/layout/macro forms,
  composite heads, dangling operators, and incomplete term introducers fail closed. Supported
  `have` is explicitly canonicalized to `let` on both paths while raw source remains unchanged.
- **Binder and structured-term safety:** surface binder and binding heads retain their original token
  class, so literals, operators, reserved words, dotted/composite spellings, and escaped/unescaped
  duplicate names fail closed. Elaborated output additionally admits Lean's printed inaccessible-name
  suffix. Bare top-level `∀` binders require one unique comma boundary; parenthesized binders preserve
  nested `∃`/`Σ`/`∀` commas. Incomplete quantifier, conditional, lambda, and `show` fragments are
  rejected at every balanced delimiter depth.
- **Surface boundary repair:** `render_surface` accepts a nonempty raw statement plus a caller-supplied
  `parsed_signature`; supplying it is the caller's attestation that the signature is complete and
  corresponds to compilable raw source in the stored context. It never guesses a declaration
  signature or proof boundary from `raw_statement`, because imported/built-in assignment syntax
  makes `:=` structurally ambiguous without Lean parsing. A successful sidecar records
  `trusted_complete_parsed_signature` in `record.warnings`; failures have no sidecar or tag. Missing signatures and unsupported
  built-in `let_fun`/`let_expr`/`let_λ`/`haveI` or generic assignment forms fail closed rather than
  truncate. The renderer intentionally does not inspect raw declarations for completeness.
- **Sorry repair:** `allow_sorry=False` now rejects a batch on `VALID_WITH_SORRY`, any nonempty
  `sorries` payload, or either canonical warning/error diagnostic spelling. The live regression
  removed the real backend's populated `sorries` tuple from an `INVALID` mixed result and still
  returned zero sidecars and two failures from the retained diagnostic alone.
- **Literal safety:** quoted literals are masked structurally with a non-whitespace sentinel, so
  delimiters inside string, guillemet, and character literals cannot masquerade as binding syntax,
  while even `""` remains a nonempty value. Surface, final-validation, static elaborated, and live
  regressions cover empty strings, delimiter-bearing strings/chars, and an incomplete char-backed
  binding that previously could appear complete.
- **Bounded loaded-environment gate:** the unchanged one-example smoke agreed exactly and retained
  request hash `147f755a4bcb9c998ea2aa5904957f654e528f97f003ad508e3a206d81c068eb`
  (1,504 ms cold, 22 ms replay). One loaded environment rendered 19/19 elaborated pilot fixtures in
  56 ms. Trusted surface signatures produced 13 exact cross-path agreements, three deliberate
  fail-closed outcomes, and three notation/name differences. Chained/nested/local bindings and
  literals agreed exactly; the bounded incomplete chain produced zero elaborated sidecars and failed
  surface validation. The separate six-source surface pilot remained 4/6 in 3 ms. Full live wall
  time was 2.04 s with 116,784 KiB peak RSS; the one-worker reservation was released.
- **Verification state:** all 203 frozen-state unit cases, source/spec hash assertions, scoped
  ruff/formatting, and strict mypy pass. No corpus was compiled, no training data was generated, and
  no durable staging output was kept.

## Superseded third-freeze risks and downstream handoff

- Surface mode is safe only when the caller attests that its supplied complete name-free signature
  corresponds to nonempty raw source compiling in the stored context. A successful sidecar carries
  `trusted_complete_parsed_signature` in `record.warnings`; the renderer does not validate this
  attestation or parse `raw_statement` to guess a signature/proof boundary. Section/autobound locals, anonymous instance binders, anonymous top-level arrows,
  shadowed names, and any source without a trusted signature must go through the elaborated path or
  fail closed. Downstream reports coverage and model metrics separately by `goal_v1_source`.
- Surface and elaborated text are not guaranteed byte-identical outside the cross-path-safe subset:
  elaboration may canonicalize `Nat` to `ℕ`, insert coercions, parenthesize binders, or sanitize local
  names. The `goal_v1_source` tag is part of the record and representation identity; consumers must not erase
  it during analysis.
- The typed/alpha fingerprint remains an optional pass-through hook. `goal_v1.0` does not compute or
  require one, and it never alpha-normalizes the model text.
- After the coherent third freeze, downstream code imports the task-owned module directly,
  constructs a complete `CompileContext`,
  batches `ElaboratedInput` values through its already-loaded backend, and sets `lookup_only=True`
  when the named theorem is already imported. It copies only `sidecar.core_text()` into pair rows and
  persists `sidecar.to_dict()` with raw compilable source. Manifests pin the final third-replacement
  spec hash and coherent freeze revision. There is intentionally no goal-to-source inverse; candidate
  workflows retain and compile their original declaration before rendering.
- A mixed inline batch may return `LeanStatus.INVALID` after some constants rendered successfully.
  Only payload-backed sidecars survive and carry `batch_had_lean_errors`; missing declarations are
  explicit failures. Use batch size one for untrusted inline candidates when per-candidate status is
  required. Infrastructure failures and any backend-reported sorry fail the batch unless
  `allow_sorry=True`.
- Surface mode supports complete, simple term `let`/`have` chains at balanced delimiter depths only
  when every assignment/body boundary is explicit with `;`. It canonicalizes the supported binding
  keyword to `let`. Layout-only bindings, same-context nested values, `let rec`, pattern/monadic
  bindings, a second top-level colon in a binding annotation, and unparenthesized
  `by`/`do`/`match`/`calc` fragments fail closed. Bare top-level `∀` binders with multiple possible
  comma boundaries also fail closed and must be written with parenthesized binders. Balanced
  parenthesized values, complete bounded `if`/`fun`/quantifier/`show` forms, common atomic symbolic
  terms, and simple parenthesized named arguments remain supported; nested structure is still
  validated. The
  raw source sidecar is always authoritative for compilation.

## Session kickoff prompt

```text
Own only REPR in /localhome/milikic/LeanFaith. Read AGENTS.md, PLAN.md,
plans/00_shared_contracts.md, and plans/02_goal_v1.md completely. Update this brief and claim only
its listed paths. Freeze goal_v1.0 with one elaborated renderer and one tagged surface fallback.
Lean is the bottleneck: do not compile a corpus; reuse loaded environments and bounded fixtures.
Preserve raw compilable source plus project/import/options context in sidecars, and never invent an
inverse from goal text. Run the one-example cross-path smoke, then the bounded multi-source pilot.
Do not generate training data. Record the spec hash, evidence, risks, and downstream handoff.
```

## Coordinator requests

- Downstream SFT/evaluation serializers must remain paused until the coherent fourth replacement
  freeze is available. Commit `cbc933c3623d81ba649a1f9c5107ad404389d69f`, spec hash
  `073d92c8e1fcc5cb7a3a9bf325d047e9b2d52149504977086de46abf6f84ef52`, and every earlier freeze
  are superseded for new pinning.
- Coordinator follow-up: mirror the scoped term-binding `have`-to-`let` exception in
  `plans/00_shared_contracts.md`. Until that coordinator-owned wording is updated, this task-owned
  spec is the explicit `goal_v1.0` normalization policy; the raw sidecar retains original notation.

## Progress log (append-only)

- 2026-08-30 — task created with the hybrid elaborated/surface decision; no renderer implemented.
- 2026-08-30 — claimed by Codex REPR session. Writable scope is limited to this brief,
  `src/leanfaith/representations/goal_v1.py`, `LeanFaith/Meta/GoalV1.lean`,
  `configs/representations/goal_v1_v1.yaml`, `tests/unit/representations/`, and
  `tests/integration/leaninteract/test_goal_v1_live.py`. Plan: finish cheap parsing/schema/provenance
  work before Lean; reuse a loaded environment for the one-example smoke and bounded pilot; never
  compile a corpus or generate training data.
- 2026-08-30 — pure implementation and schema gate passed: frozen compile-context/sidecar records,
  deterministic theorem/lemma surface parser, explicit fail-closed outcomes, spec/config hash check,
  proof-leak checks, and six-source fixtures; 14/14 owned unit tests passed before Lean.
- 2026-08-30 — one-example elaborated/surface smoke passed exactly, including raw/context join and
  identical replay request hash. The same one-worker loaded fixture environment then passed the
  bounded six-row elaborated pilot and six-source surface pilot. No corpus compile or training-data
  generation occurred.
- 2026-08-30 — froze `goal_v1.0` at spec hash
  `690d57348e2098dd7aeda10eede6cec9182aa002eb69ec910ef3d47269ea160c`; targeted formatting,
  strict typing, 14 unit tests, and one live integration gate all passed. Recorded risks and
  downstream handoff above, released the REPR host reservation, and marked the task complete.
- 2026-08-30 — independent Claude/user review rejected the candidate freeze and reopened REPR as
  `active`. Acceptance blockers: preserve reducible conclusions during telescope introduction;
  apply the declared render width; apply structured compilation context; provide a true lookup-only
  path for already-loaded theorem constants; qualify the ConsistencyCheck fixture as derived; add
  regression fixtures; recompute the spec hash; rerun the cross-path smoke and bounded pilot; and
  commit all REPR-owned files. No corpus compilation or training-data generation is authorized.
- 2026-08-30 — closed the technical review blockers at replacement spec hash
  `2fc5b69c0534449d4ffeca0f47fddec38042fff90de374b3bda81d4f25dd23d8`: the Lean renderer now
  preserves reducible conclusions and renders at width 1,000,000; structured context is applied;
  imported theorem constants have a lookup-only path with retained raw sidecars; helper payloads
  enforce theorem kind; surface equation proofs fail closed; and sorry policy is enforced. The
  resource-claimed live gate passed the exact cross-path smoke, ten-row elaborated pilot, and
  six-source surface pilot in one loaded environment. The reservation was released; the task remains
  `active` until the REPR files are committed and the committed freeze record is written.
- 2026-08-30 — committed every REPR implementation/spec/test path at
  `b871900e5177bbda38471f98cc46818bb6502b0d`, then wrote the replacement freeze record and marked
  REPR `complete`. No training data was generated; downstream consumers may pin the replacement spec
  hash and reviewed implementation revision while retaining raw/context sidecars.
- 2026-08-30 — user/live review reopened REPR before downstream use. A mixed `INVALID` batch could
  retain a sorry-backed sidecar despite `allow_sorry=False`, and a valid top-level `let` proposition
  was silently truncated by surface extraction while elaborated multiline output failed validation.
  The prior freeze is superseded; no corpus rebuild is authorized. REPR remains `active` until both
  cases have unit/live regressions, updated hashes, bounded verification, and replacement commits.
- 2026-08-30 — second repair candidate passed: a live mixed-invalid/sorry batch returned 0 sidecars
  and 2 failures with sorry disallowed; a valid top-level let theorem rendered exactly as
  `⊢ let x := 1; x = 1` on both paths. The loaded-environment pilot passed 11/11 plus the safety
  request, the six-source pilot remained 4/6, 25 active-state unit cases passed, and scoped
  formatting, ruff, strict mypy, and plan validation were clean. The reservation was released; REPR
  stays `active` until the replacement implementation is committed and the final freeze record is
  committed afterward.
- 2026-08-30 — committed the second replacement implementation at
  `75923b3ca5aa3915c0fee1278f45b547d9c96e3c`, froze spec hash
  `7ec7b82923b4eb78a737f47653dfc7d7b5eb619373159ec1cf5ed0d794759ae9`, and marked REPR `complete`.
  The final record includes the canonical let policy, batch-wide reported-sorry policy, 26/26 unit
  tests, the bounded live evidence, source hashes, risks, and downstream handoff. No training data or
  corpus build occurred.
- 2026-08-30 — user and Claude Fable-5 independently rejected the second freeze. Surface extraction
  can truncate chained/nested `let` or `have` targets at an internal `:=` and then accept the
  incomplete goal; the canonical diagnostic that a declaration uses `sorry` can bypass the batch
  policy when `result.sorries` is empty. The third repair must structurally validate complete binding
  chains, cover chained/nested/incomplete cases on both paths, freeze the elaborated `have`-to-`let`
  policy, recognize canonical sorry diagnostics, and produce a single coherent consumable freeze
  revision. REPR is `active`; no downstream use, corpus build, or training-data generation is
  authorized.
- 2026-08-30 — third repair candidate passed the resource-claimed loaded-environment gate. The
  unchanged smoke agreed exactly with identical request hashes; 18/18 elaborated pilot fixtures
  passed, including exact cross-path chained `let`/`have` and nested existential, universal, and
  conjunction cases plus empty-string and delimiter-bearing character bindings. The incomplete-chain
  fixture failed closed on both paths, character-literal separators fail closed, and a real mixed
  `INVALID` result with its structured sorry payload removed still returned zero sidecars from the
  canonical diagnostic alone. The six-source pilot remained 4/6. No corpus or training data was
  produced, and the reservation was released; REPR stays `active` through the coherent commits.
- 2026-08-30 — final adversarial repair removed raw declaration boundary inference from the public
  surface contract. Surface mode now requires a tagged trusted complete parsed signature and retains
  raw compilable source only in its sidecar, so loaded/custom assignment syntax cannot masquerade as
  the theorem proof boundary. Original-token heads, structured fragments, common atomic symbols,
  colon-only elaborated locals, diagnostic-only sorry results, and malformed/incomplete inputs have
  bounded regressions. The final loaded-environment gate passed the unchanged smoke and 19/19 pilot
  in one request (13 surface agreements, three tagged failures), while the six-source pilot remained
  4/6. No corpus or training data was produced, and the reservation was released.
- 2026-08-30 — correction and final adversarial gate: surface input is a caller-attested
  `parsed_signature`, not a tagged input; only successful sidecars append
  `trusted_complete_parsed_signature` to `record.warnings`, so the three pilot failures have no tag.
  The structural validator now rejects malformed binder tokens, ambiguous bare-`∀` comma boundaries,
  dangling operators/commas inside balanced delimiters and structured-term segments, incomplete
  quantifier/conditional/lambda/`show` forms, and non-single named-argument parentheses. The final
  resource-claimed gate passed the unchanged smoke hash, 19/19 elaborated fixtures in one loaded
  request (13 exact surface agreements and three fail-closed outcomes), and the 4/6 six-source
  pilot. All 203 owned unit cases pass after final repinning; wall time was 2.04 s with 116,784 KiB
  peak RSS. Raw compilability/correspondence remains an explicit caller attestation, no corpus or
  training data was generated, and the reservation was released.
- 2026-08-30 — committed the third replacement implementation at
  `fc612cb0816e83a8b29625b67988d95e444a7eb2` with the config and matching unit assertion still
  `active`, then completed REPR in the coherent child commit containing this freeze record, frozen
  config, and frozen-state assertion. Only that child commit is consumable downstream. The frozen
  spec/source hashes and bounded evidence above were rechecked; unrelated work remained untouched.
- 2026-08-30 — user reopened REPR because coherent freeze
  `cbc933c3623d81ba649a1f9c5107ad404389d69f` supports named constants and caller-attested surface
  signatures but is not consumable by SFT1's already-certified direct `Expr` candidates. The fourth
  repair must expose one public `renderClosedProp`, delegate named constants to it, batch reference
  and candidate Expr rendering in one Meta request without declarations/proofs/re-elaboration,
  freeze Expr provenance and one universe profile, add a runtime Lean-helper hash assertion, and
  raise the pinned 859-row ConsistencyCheck elaborated-goal coverage with targeted rules. Lean is
  the bottleneck: all safe API/schema/validator work and the 859-row Lean-free audit precede one
  bounded shared-environment live gate; no corpus compile or training-data generation is authorized.
- 2026-08-30 — completed the fourth repair's Lean-free gate before invoking Lean. The shared
  `renderClosedProp` API, literal named delegate, exact direct-Expr/source-material sidecars,
  recursive Expr/level validation, runtime helper pin, one first-occurrence `u_i` profile, atomic
  post-validation, and declaration-free Meta-action admission are implemented. The targeted
  structural validator now accepts all 859/859 pinned ConsistencyCheck `goal` strings (up from
  804/859) while malformed bar, postfix, image, delimiter, universe, structure, and binding
  near-neighbors fail closed. All 291 owned unit cases, scoped ruff/formatting, strict mypy, and the
  plan validator pass. No Lean, corpus compilation, or data generation occurred during this gate;
  the next action is the one-worker shared-environment live oracle.

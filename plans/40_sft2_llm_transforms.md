# SFT2A — LLM-generated semantic transformations

> **Task ID:** SFT2A
> **Status:** pilot_ready
> **Owner/session:** Codex `/root` — 2026-08-31 SFT2A v5 session
> **Last updated:** 2026-08-31
> **Dependencies:** REPR `goal_v1.0`; shared rubric; roots may be selected independently of SFT1
> **Next gate:** exact user authorization for only the corrected 100-root/400-slot v5.1 rehearsal
> **Compute class:** external LLM/API plus CPU/RAM for Lean; large run may need explicit budget approval
> **Lean budget:** compile each novel candidate once through cached persistent workers
> **Local staging root:** `/storage/milikic/leanfaith/value_first/sft2_llm_transforms_v1/`
> **HF destination:** private `Lemmy00/leanfaith-sft2-llm-transforms-v1`

## Objective and scale

Select 50K diverse theorem roots and ask Codex for two meaning-preserving and two meaning-breaking
Lean statement transformations per root. Compile every candidate, then use Claude as an independent
semantic judge. Retain each successful candidate independently; quality is more important than
forcing exactly four accepted rows per root.

## Root sources and active authorization

Sample theorem/lemma roots from Mathlib, Physlib, CSLib, and valid theorem-shaped rows of
`formalmathatepfl/compiler_data`, with a source/domain/signature-structure freeze in the pilot.
Mathlib/Physlib/CSLib use the pinned contexts in `configs/projects/{mathlib,physlib,cslib}.yaml`.
Compiler-data roots retain their source imports/context and are skipped when a safe project context
cannot be established; never guess one or compile the full source corpus.

External-model processing of `formalmathatepfl/*` is explicitly authorized by
`policies/source_use_v2.yaml`. This task creates additive v2 source configs under its own config
directory with `policy_version: source_use_v2` and `external_transmission: true`; it never edits the
fail-closed v1 source configs used by historical runs.

## Label workflow

For each root and candidate slot:

1. Provide Codex the `goal_v1.0` reference, raw compile context needed to write a valid statement,
   the requested polarity, and the consistency rubric from `00_shared_contracts.md`.
2. Require an informative transformation, not cosmetic churn. Ask the proposer to identify the
   intended semantic mechanism and likely trap for a learned judge.
3. Compile/elaborate the candidate once. Store the verdict. Invalid candidates are not semantic
   negatives; they remain in the auxiliary validity/attempt sidecar and the slot may be retried.
4. Give Claude a blinded reference/candidate pair and the full consistency rubric. Do not reveal
   the requested label. Require equivalent, non-equivalent, or unknown with a short structured
   rationale and relation/error type.
5. If Claude agrees with the requested polarity, retain the compiled pair with that binary label.
   If it disagrees or returns unknown, retry that slot up to three total attempts.
6. Preserve every accepted candidate even when sibling slots fail. After three failed attempts,
   mark only that slot unresolved; do not discard the root or accepted siblings.

The core schema is `{reference, candidate, label}` in `goal_v1.0`. A keyed sidecar stores proposer
model/version/effort, prompt hash, requested polarity, generation attempt, compilation cache key,
Claude model/version/effort, vote/rationale, relation class, raw references, and lineage.

For new generated rows, the user's “double agreement” is proposer-requested polarity plus one
blinded Claude judgment; record the precise basis as `proposer_intent+single_judge`, not two
independent semantic judges. Send a blinded 10% stratified sample to Lemex as a second-judge audit.
Systematic disagreement blocks scale; audited disagreements route to unknown/review unless resolved.

## Prompt requirements

Both prompts must define same intended mathematical claim carefully: equivalent is stricter than
both statements being true or sharing concepts; one-way implication is insufficient. Cover binder
types/domains, dependent quantifier order, assumptions and conclusion strength, coercions,
existence/uniqueness, boundary cases, converses, negation scope, and representation-only changes.
Ask for high-information hard cases across multiple mechanisms and reject output that merely
renames a theorem or makes a trivial whitespace change.

Freeze prompt versions and structured output schemas after adversarial one-root examples. The
production Claude model/effort and Codex model/effort must be pinned in the run manifest after a
quality/cost pilot; the separate repository-setup review does not choose those settings.

## Existing assets and reuse

- Use the resumable runner style in
  `/localhome/milikic/annotate_numina/run_reasoning_direct.py`: isolated workers, locked/append-only
  output, stable IDs, bounded retries, and deterministic final compaction.
- Reuse the repository's `collect2/`, provider invocation, prompt schema, Lean backend/cache, and
  journal components where contracts fit; do not create a second ad hoc executor.
- Import the 13,373 compiled Qwen/Kimi + Codex-judged records as a
  `legacy_single_judge` tranche after DATA-REUSE confirms hashes/schema:
  `/storage/milikic/leanfaith/corpus2/recovered_singlepass_codex_v1_e8567ba/outputs/trainer_records.jsonl`.
- For high-quality double agreement, send all 233 REPR-admitted legacy positives and at least 2K
  stratified negatives to Claude. Eligible/renderable unresolved records have no prior binary label,
  so retain them in a `legacy_single_judge_needs_second_judge` auxiliary view without an Opus call;
  do not pay for a judgment that this runner would necessarily discard. Accepted double-judged rows
  may join the core; keep the rest explicitly legacy rather than pretending they have two judges.

## Scope and ownership

**In scope:** root sampling, prompt/rubric design, provider wrappers, per-candidate compilation and
judgment, retries, legacy rejudging, core/sidecar serialization, manifests, and private publication.

**Out of scope:** deterministic SFT1 generation, autoformalizer NL translation, model training,
using invalid Lean as label `0`, or discarding accepted candidates due to incomplete siblings.

**Writable paths:** this brief; `src/leanfaith/sft2a/`; `configs/sft2a/`; `prompts/sft2a/`;
`tests/unit/sft2a/`; the staging root. Existing `collect2/`, generation/providers, corpus2, shared
Lean/project/dependency paths, and other tasks are read-only reusable interfaces; request
coordinator changes.

**Paths claimed by this session:** `plans/40_sft2_llm_transforms.md`;
`src/leanfaith/sft2a/`; `configs/sft2a/`; `prompts/sft2a/`; `tests/unit/sft2a/`;
`/storage/milikic/leanfaith/value_first/sft2_llm_transforms_v1/`. No shared Lean, provider,
project, dependency, policy, or other task path is claimed.

## DATA-REUSE legacy decision

**Accepted with the explicit placeholder exclusion** for a separate `legacy_single_judge`
configuration. The immutable source root is
`sha256:d49bad5cbe0f8a19ff76e285d958503d4c96d80afaf2571497ee1988ad970622`.
Inspect all 13,367 resolved rows without relabeling, deduplicate on the raw directed
`(reference_headless, candidate_headless)` pair, and retain the lexicographically smallest immutable
`record_id` in each group. This removes the seven excess directed-pair rows deterministically. A
pre-import scan found zero `[anonymous]` rows and 144 `⋯` rows, none overlapping the duplicate
excess; those 144 rows are rejected to a separate legacy-placeholder audit view. This leaves 13,216
rows eligible for frozen REPR adaptation (297 positive, 12,919 negative). The fail-closed surface
adapter accepts 10,333 of them (233 positive, 10,100 negative); 2,883 rows (64 positive, 2,819
negative) use syntax outside the surface renderer's explicit contract, predominantly untyped
binders, and remain in the separate invalid view. Compiling 2,883 legacy rows merely to recover them
is not authorized under the Lean-bottleneck contract. The six unresolved judgments remain keyed
sidecar-only and never become binary rows. The legacy representation is adapted through frozen
`goal_v1.0` before any headline mixing, and its label basis remains
`qwen_or_kimi_proposer+single_codex_judge`; it is never described as new double agreement.

## Lean-efficiency plan

Lean is the bottleneck, but candidate compilation is required here. Run cheap schema/placeholder/
declaration-shape checks first. Group homogeneous projects/imports, prepare each environment once,
batch through persistent LeanInteract workers, and cache every terminal verdict by candidate,
context, Lean/project, options, and checker version. Reuse the verdict for goal rendering and all
judges; never compile separately for each vote. Retry only infrastructure failures.

Start with one worker and measure 4/8 under concurrent-session load. Keep `Elab.async=false`,
bounded batches, isolated requests, ordered results, and append-only attempt journals. Ask for CPU/
RAM capacity—not GPU—if Lean dominates; ask for larger compute/API budget if model inference does.

## Execution gates

### One-example smoke

One root must exercise four independent slots, structured parsing, compile cache, blinded Claude
judgment, accepted-sibling preservation, a rejected-slot retry, `goal_v1.0` rendering, final core row,
sidecar, journal, and restart without duplicate calls.

### Pilot

Run a diverse bounded root sample and the legacy double-judge sample. Report accepted rows/slot,
polarity balance, relation diversity, invalid/unknown/disagreement/retry rates, judge agreement,
Lean cache and throughput, LLM cost/latency, duplicate rate, and projected 50K-root cost. Review a
stratified human-readable sample before scale.

### Scale and publication

Scale only with explicit recorded model/prompt/budget settings. Use resumable root/slot shards and
deterministic compaction. Publish core, legacy, invalid-attempt, and rich-judgment configurations or
sidecars with unambiguous names. Screen both reference/candidate signatures against
`data/benchmarks/golden_blocklist_v1.json` using exact and near-duplicate keys; exclude hits from
training. Validate hashes/counts and the remote Hub revision.

## Acceptance criteria

- Candidates are compiled and judged independently under a frozen, careful consistency rubric.
- Accepted candidates survive sibling failures; retries are capped at three per slot.
- Invalid candidates are auxiliary, while valid non-equivalent candidates are core negatives.
- New core rows record proposer-intent + blinded-judge agreement and a 10% second-judge audit;
  legacy single-judge rows are visibly separate.
- All calls/checks are resumable, cached, versioned, costed, and reproducible.
- 50K-root scale is not started until the pilot quality/cost report is accepted.

## Session kickoff prompt

```text
Own only SFT2A in /localhome/milikic/LeanFaith. Read AGENTS.md, PLAN.md,
plans/00_shared_contracts.md, and plans/40_sft2_llm_transforms.md completely. Update this brief and
claim exact paths. Design and freeze careful Codex proposer and blinded Claude judge prompts, then
run exactly one root through four independent slots before any pilot. Preserve every accepted slot;
retry only its rejected slot up to three times. Lean is the bottleneck: compile each candidate once
through persistent cached workers and reuse the result. Invalid is not semantic false. Keep core,
legacy, unknown, and invalid views distinct. Do not launch the 50K run without a measured pilot and
recorded budget/model decision.
```

## Coordinator requests

- Approve the bounded diverse-root sample size/source mix and legacy double-judge spend before the
  pilot begins; the one-root smoke is not used to project 50K-root quality or cost.
- Approve pinned production proposer/judge settings and projected API budget after the pilot.

## Progress log (append-only)

- 2026-08-30 — task brief created; no LLM or Lean calls made.
- 2026-08-30 — Codex `/root` claimed only the SFT2A brief, package, config, prompt, test, and staging
  paths. Lean remains the bottleneck: all schema, placeholder, source, provenance, join,
  deduplication, and cache-key work precedes one reserved persistent Lean worker; no corpus compile,
  50K-root run, or publication is authorized. Accepted the hash-backed DATA-REUSE legacy recipe:
  keep 13,367 resolved rows in a separate single-judge view after removing seven excess directed
  duplicates by deterministic record-ID tie-break, and retain six unknown rows sidecar-only.
- 2026-08-30 — the required placeholder screen found zero `[anonymous]` rows and 144 resolved rows
  containing `⋯`; none was one of the seven duplicate excess rows. The legacy decision is accepted
  only with those 144 rows excluded to a separate audit view, leaving 13,216 binary legacy rows
  (297 positive and 12,919 negative). Placeholder-bearing rows are not semantic negatives.
- 2026-08-30 — materialized the accepted legacy recipe and replayed it byte-identically. Frozen
  REPR admitted 10,333 single-judge rows (233 positive, 10,100 negative); 2,883 surface-render
  failures remain invalid/auxiliary, the 144 placeholder rows remain audit-only, all six unknowns
  remain sidecar-only, and the gold blocklist produced zero hits. No Lean or external model was
  invoked for this import.
- 2026-08-30 — froze strict Codex proposer and blinded Claude/Lemex judge prompts plus JSON schemas,
  all fourth-freeze REPR identifiers and implementation hashes, provider CLI bytes/settings, the
  Mathlib context, and the 5,111-row gold screen. Implemented one-Expr proof-free proposition
  elaboration, persistent content-addressed Lean/provider caches, four independent slots, a
  three-attempt per-slot cap, accepted-sibling preservation, separate new-core/legacy/invalid/
  unknown/rejected/contamination/audit views, and immutable replay receipts. Unit simulation
  exercised one rejected-slot retry without rerunning accepted siblings.
- 2026-08-30 — reserved one Lean worker/20 GiB only after the SFT1 reservation cleared, then
  released it immediately after the bounded root. The exact Mathlib `le_trans` root produced four
  valid gold-clean candidates, all elaborated once and cached: two preserving and two breaking;
  blinded Claude accepted all four with high confidence. The live invalid, unknown, semantic
  disagreement, duplicate, and gold-hit counts were all zero, so no live semantic retry was
  triggered. The deterministic one-root unit scenario separately forces one rejected first attempt,
  preserves the other accepted siblings, and accepts only that slot's second attempt under the
  three-attempt cap.
- 2026-08-30 — one infrastructure-only Claude retry was needed because CLI 2.1.251 cannot resolve
  the standard draft-2020-12 `$schema` annotation. The failed capture is durable and created no
  judgment; the identical candidate was retried after omitting only that non-validating transport
  annotation. Successful Claude cost was $0.218831; Codex CLI cost is unavailable. Across the four
  unique candidates, original Lean elapsed time was 23.922 seconds (0.167 candidates/second), not
  including environment startup; one candidate was a cache hit when the interrupted orchestration
  resumed. These one-root figures are smoke evidence, not a scale projection.
- 2026-08-30 — repeated one-root replay produced zero Lean requests, zero provider calls, zero
  duplicate outputs, and stable snapshot hash
  `3a5181a363ed29964a8a56e73c89fe0ddcff71c7d57c072e3949736a5207669c`.
  Only after that receipt, the frozen 10% stratified Lemex audit selected two of four rows because
  each small stratum rounds up to one; both agreed with Claude, with zero unknown-review rows. Audit
  replay was also cache-only. The bounded diverse pilot, 50K-root run, and publication remain
  unstarted; no statistically defensible scale/cost projection is claimed from one root.
- 2026-08-30 — review requires fixes before pilot authorization. The Fable config and its one-root,
  replay, audit, and provider receipts are sealed as immutable historical smoke evidence. SFT2A is
  active again; the next gate is an additive Opus one-root smoke that reuses shared proposer/Lean
  caches. No diverse-root pilot, legacy bulk rejudging, publication, or 50K run is authorized.
- 2026-08-30 — added the judge/config-scoped `one_root_opus5_v1` run without changing the Fable
  config or historical output trees. Provider and Lean caches remain under the shared staging root;
  derived Opus, comparison, pilot, legacy-rejudge, and release artifacts have versioned run roots.
  The Opus pin is Claude CLI `2.1.251`, binary
  `sha256:fd5f10ff0eb58daec04900466b143ea98aab50abf208a422bc008eaec13f61f7`,
  alias `opus`, effort `max`, and distinct provider ID
  `claude_opus_alias_max_sft2a_smoke_v1`; the server alias remains explicitly floating.
- 2026-08-30 — the bounded Opus `mathlib:le_trans` smoke accepted the same four candidates on their
  first attempts: two preserving and two breaking. It executed exactly four new Opus judgments,
  while all four proposer calls and all reference/candidate Lean requests were cache hits. There
  were zero invalid, unknown, disagreement, retry, duplicate, or contamination outcomes. Reported
  Opus spend was $0.100341 and recorded provider latency was 62.352945 seconds. Replay executed zero
  provider/Lean calls with snapshot hash
  `13aa171791110056d1414ac8618bd22f70a9bb47472d7eccccfe06b967122f98`.
  The side-by-side Fable/Opus join found the same four candidates and 4/4 verdict agreement; Fable
  reported $0.218831 over 64.578945 seconds. The Fable trees remain immutable.
- 2026-08-30 — implemented the deterministic four-source sampler and grouped runner, but did not
  execute it. The exact proposed pilot contains 12 roots: five Mathlib, three Physlib, two CSLib,
  and two safe-context compiler-data roots. Execution is grouped into Mathlib, Physlib, and CSLib
  persistent contexts. Hard ceilings are 12 roots, three attempts per slot, 144 Codex calls, 144
  Opus calls, 288 total provider calls, zero Lemex calls in generation, and $15 reported Opus spend.
  The separately authorized post-replay 10% audit is proposed at no more than eight Lemex calls,
  making the end-to-end provider-call cap 296. Codex and Lemex cost remain unavailable. The config
  keeps pilot and audit authorization false.
- 2026-08-30 — implemented but did not execute the legacy Opus-rejudge command. Its hash-bound
  sample has all 233 REPR-admitted positives, 2,000 deterministic family-stratified REPR-admitted
  negatives, and all three eligible/renderable unresolved pairs (2,236 total); placeholder,
  REPR-invalid, and contamination views receive no Opus calls. The distinct
  `legacy_double_judge` path is hard-disabled pending approval, with ceilings of 2,240 Opus/total
  calls and $250 reported Opus spend. `legacy_single_judge` remains unchanged.
- 2026-08-30 — materialized the historical Fable post-audit releasable core by stable row ID: four
  minimal `{reference, candidate, label}` rows and zero excluded IDs because the historical audit
  had zero disagreements. The exporter and unit regression exclude a disagreed ID before writing
  core. New audit manifests record source-run hash, full source-Opus and Lemex pins, prompt hashes,
  usage, latency, cost, and explicit unavailable-cost limitations. No new Lemex audit was run.
- 2026-08-30 — final readiness review replaced the prior near-duplicate/trivial pilot roots with
  varied theorem structures while preserving the exact 5/3/2/2 source mix. The hash-bound sample is
  `d0568942cf276939a47b375a73715fcae489a9b9c380c9aa02bd780bd706ba75`: Mathlib `le_trans`,
  `List.reverse_reverse`, `Nat.add_comm`, `Nat.gcd_comm`, and `Set.union_comm`; Physlib free-particle
  energy conservation, FLRW asymptotics, and CKM row normalization; CSLib register write/read and
  timed merge semantics; and compiler-data real-analysis optimization and factorial Diophantine
  classification. The pilot remains unexecuted and groups the 12 roots into three persistent
  project environments.
- 2026-08-30 — added an append-only provider ledger that preserves cumulative Opus calls and spend
  across process restart, requires every Opus result to carry a cost report, and enforces the frozen
  $15 pilot ceiling after resume. Added a persistent cross-root raw/rendered candidate registry and
  a consolidated pilot quality/cost/throughput/projection manifest plus report. The additive
  authorization receipt binds the exact sample and ceilings and remains `authorized: false`.
- 2026-08-30 — corrected the earlier legacy readiness proposal: the 2,233 provider-call rows are
  exactly 233 admitted positives plus 2,000 deterministic stratified admitted negatives. The three
  eligible/renderable unresolved rows are now a distinct
  `legacy_single_judge_needs_second_judge` auxiliary view with `provider_call_allowed: false`; they
  never enter the Opus request set. Legacy rejudging remains disabled.
- 2026-08-30 — the user clarified that model spend is not the practical limiter while model tokens
  remain available and models should be used according to task need. The $15 pilot ceiling remains
  in this readiness version as a reproducibility and authorization contract, not as an inferred
  affordability constraint; changing it requires a new additive hash-bound authorization artifact.
- 2026-08-30 — preserved the historical Fable combined-tree seal algorithm and hash in a repository
  receipt, left the frozen Fable and Opus smoke bytes unchanged, formatted all reported SFT2A files,
  and passed the 14 SFT2A tests, Ruff check/format verification, strict Mypy, frozen-config checks,
  readiness verification, and diff checks. No provider, Lean, diverse-root, legacy, publication, or
  50K execution occurred during final readiness work.
- 2026-08-30 — added the production-default configuration without changing either frozen smoke. It
  is hash-bound to active policy
  `4554a071b06b1af9015b253b5e64b2a0a4d013630e5224ef7729bbf65757646f` and pins Claude Code
  Opus 5/high, `gpt-5.6-terra`/high, and `moonshotai/Kimi-K2.7-Code`/high under distinct provider
  IDs with exact local CLI versions and binary hashes; all server aliases remain honestly marked
  floating. Shared provider and Lean caches remain outside the versioned production run trees.
- 2026-08-30 — ran exactly one authorized production-settings root, Mathlib `le_trans`, through
  four slots. It accepted two preserving and two breaking candidates on four first attempts with
  zero invalid, unknown, judge-disagreement, duplicate, retry, or contamination outcomes. Four new
  Terra calls and four new Opus-high judgments ran; reported Opus spend was $0.083279 and provider
  latency was 48.188734 seconds. Lean is still the bottleneck: cheap filtering preceded one claimed
  persistent worker, one novel candidate executed in 15.877 seconds, three candidates hit the
  shared cache, and the 1-worker/20-GiB claim was released immediately afterward. Replay executed
  zero provider calls and zero Lean requests with snapshot
  `80f5f856166c43394df938256b57fd36be7d669735eb7a0012cdb5ad33232814`.
- 2026-08-30 — completed only the exact one-root smoke's Kimi-high blinded audit: deterministic
  small-stratum rounding selected two of four accepted rows, both agreed with Opus, cost reporting
  remained explicitly unavailable, and immutable audit replay made no additional call. Added the
  pilot-specific completed-run replay receipt, which forbids provider/Lean execution and seals all
  durable hashes, plus the combined-pilot 10% stratified audit capped at eight calls on the same
  persistent 296-call ledger. Forced-disagreement regression routes the row to unknown/review,
  excludes its stable ID from releasable core, propagates audit hashes into the consolidated report,
  and blocks scale.
- 2026-08-30 — prepared but did not launch the production pilot's named detached `tmux` path. It
  requires a clean committed implementation, the exact config/readiness/authorization hashes, one
  exclusive run lock, one host-wide Lean resource claim, closed stdin, a persistent combined log
  and append-only journal, explicit resume and health commands, and duplicate-start refusal. The
  production receipt is `authorized: false`; the next gate is user authorization for the 12-root
  pilot only. The 2,233-row legacy rejudge, publication, and 50K run remain unauthorized.
- 2026-08-30 — fixed the authorization-transition collision without editing the frozen
  readiness-only receipt/config or its staged sample. The additive v2 activation plan verifies the
  old `pilot_authorized: false` manifest and exact hashes, requires the full authorization sentence,
  and only then can materialize new authorized receipt/readiness files. Those targets remain absent,
  so launch is still disabled. The authorized transition uses fresh output
  `runs/diverse_root_production_defaults_pilot_v2` and `tmux` session
  `leanfaith-sft2a-production-pilot-v2`; preflight writes the same hash-bound 12-root sample there
  with zero provider/Lean execution and stops before starting `tmux`. A regression proves the
  current unauthorized state fails closed, the prospective authorized model passes
  `require_pilot_authorization`, the old sample stays byte-identical, and the fresh-root preflight
  reaches the detached boundary. Legacy rejudging, publication, and 50K remain unauthorized.
- 2026-08-30 — separated SFT2A from the accidental combined history: branch
  `milikic/sft2a-production-activation` starts at `57a63e3` and cherry-picks only the SFT2A
  production-readiness commits before this activation fix. The two SFT2B setup commits are not
  ancestors of this branch and must not be silently merged as part of SFT2A integration.
- 2026-08-30 — received the exact hash-bound authorization for only the 12-root production-default
  pilot. While clean at activation commit `5055134`, materialized the additive v2 authorization
  receipt `e00195d887692fe309ec024f46d52867b9ec6b2bd52488fdf4b8f465e9ea0b6c` and readiness file
  `fefdc00a8e694974fe75a64295a78122ac5f5083d036c99d3a1fbb3d90c58473` with effective readiness
  hash `392e220d10ecb224b9ca061a02ca838d60626ab55c389a4183af5e345c5ae8c2`. The authorized path retains
  sample `d0568942cf276939a47b375a73715fcae489a9b9c380c9aa02bd780bd706ba75`, the 296-call/$15 Opus
  ceilings, fresh v2 output, and versioned `tmux` session. This authorization explicitly excludes
  the 2,233-row legacy rejudge, publication, and 50K run.
- 2026-08-30 — the first authorized detached startups reached `worker_started` but received tmux
  `SIGHUP` before the resource claim because redirecting stdin/stdout/stderr removed every open
  reference to the pane PTY. They executed zero provider and Lean calls and held no reservation.
  Preserved their sample, launch requests, and journal under additive evidence root
  `runs/diverse_root_production_defaults_pilot_v2_failed_startup_15d6a26`. The worker now retains one
  non-I/O PTY descriptor for its lifetime, writes an initial log record, and startup health requires
  the named live pane, held run lock, and matching one-worker/20-GiB resource claim rather than old
  journal-row count. The authorized sample/config/ceilings and v2 tmux name are unchanged.
- 2026-08-30 — the authorized v2 pilot completed nine roots durably, then failed closed before any
  Physlib provider call on `physlib:ckm_row_norm`. The frozen catalog had incorrectly qualified the
  global Physlib declarations `CKMMatrixSetoid` and `VAbs` as members of namespace `CKMMatrix`;
  the theorem itself is correctly named `CKMMatrix.VAbs_sum_sq_row_eq_one`. The worker recorded a
  terminal failure, released its one-worker/20-GiB claim, and left no process or tmux session.
  Preserved the full v2 sample, manifests, 73-call cumulative ledger, journal, log, and terminal
  bytes. A resource-claimed one-signature oracle proved that removing only those two namespace
  qualifiers elaborates successfully through frozen REPR.
- 2026-08-30 — added an unauthorized recovery overlay rather than editing the frozen catalog or
  failed v2 run. It produces corrected sample
  `52edf04e5cfddefcd6626dfcb0ee0785f4a0f1e9dbd4cfd0851407e6134ccea4`, binds correction receipt
  `6a562f4b9e397ede3b8096ba1ce3d59bee977ee6dd66a0dac8133ce67f3f54b6`, and seeds the recovery
  ledger byte-for-byte from failed-run ledger
  `b5b661ec4828f6616a9e4379d1ca7dcbd9f01100a7c7a23bb5dbd4bbff1ca8e9` so restart cannot reset
  the 296-call or $15 ceilings. Its staged readiness sample is bound to clean implementation
  `71319fa`; a versioned v4 activation targets fresh output
  `runs/diverse_root_production_defaults_pilot_recovery_v4` and tmux session
  `leanfaith-sft2a-production-pilot-recovery-v4`. The original authorization was bound to the old
  sample, so recovery launch remains fail-closed pending a new exact user sentence. Legacy
  rejudging, publication, and 50K remain unauthorized.
- 2026-08-30 — received the exact hash-bound authorization for only the corrected 12-root recovery
  pilot. While clean at activation commit `c8287a5`, materialized the additive v4 authorization
  receipt `ecd0b2c21e2a1ca7aab34d86dd9c869472807d28a8beefa4b28e5a1c5ac314b6` and readiness file
  `2a2c3e275c40c2c1cfed3ba80389cc5b788e753a9c40a9d362ad638e5b236a79` with effective readiness
  hash `a094a9e6ef365c7c769c6759c34d6dd61f7ffd176e11374b177190761a4e4a58`. The authorized path is
  bound to corrected sample `52edf04e5cfddefcd6626dfcb0ee0785f4a0f1e9dbd4cfd0851407e6134ccea4`, production config
  `2f9aafb0f36a1cc01734a02e8197b308b940efac4f0681ba306c9dd9cb0a7877`, correction receipt
  `6a562f4b9e397ede3b8096ba1ce3d59bee977ee6dd66a0dac8133ce67f3f54b6`, and cumulative failed-run
  ledger `b5b661ec4828f6616a9e4379d1ca7dcbd9f01100a7c7a23bb5dbd4bbff1ca8e9` under the existing
  296-call/$15 Opus ceilings. Legacy rejudging, publication, and 50K remain unauthorized.
- 2026-08-31 — accepted recovery-v4 only as immutable historical evidence and sealed its 137
  regular files with combined-tree hash
  `3ea4a72280c696b1811995b51403373384ae8269ee05accc351f9527d82dd06a`. Added an additive v5
  contract under active Opus-high/Terra-high/Kimi-high defaults. V5 rejects raw shortcuts before
  Lean and rejects closed-Expr or rendered-`goal_v1` identity before judging. It adds exact
  closure-equivalence canaries for `nat_add_comm/break_1`, `set_union_comm/break_1`, and
  `nat_gcd_comm/break_0`, one malformed-judge retry for verdict/rationale contradictions, and
  separate genuine-disagreement routing. Recovery-v4 bytes were not changed.
- 2026-08-31 — completed the source-text-only v5 census with zero provider calls and zero Lean
  requests: 178,673 eligible distinct signatures across Mathlib, Physlib, CSLib, and safe-context
  compiler-data inputs. Froze a domain/shape/source-stratified 100-root sample with source mix
  42/25/17/16 and 400 planned slots, applicability-aware mechanism assignments, 13 preserving and
  14 breaking families, maximum observed family shares 14% and 9%, and deterministic project
  shards. The sample, rehearsal, audit, detached launch, and 50K projection code remain fail-closed
  for calls until an additive hash-bound rehearsal authorization is received. The approximately
  10K gate, 50K run, legacy rejudge, and publication remain separately unauthorized.
- 2026-08-31 — passed the closure-aware v5 live gate on implementation `92ddfb4` (tree
  `15c0425`). Opus-high correctly classified all three closure canaries as equivalent with no
  malformed response and no Lean request. The `mathlib:le_trans` smoke then accepted two
  preserving and two breaking candidates on their first attempts, with zero self-pairs,
  cross-root duplicates, contamination, invalidity, unknowns, malformed judgments, or semantic
  disagreements. Four candidates executed once through frozen REPR in 8.424 seconds; replay
  executed zero provider calls and zero Lean requests. The smoke used four Terra calls and seven
  Opus calls including canaries, with $0.230928 total reported Opus spend. Receipt
  `configs/sft2a/closure_aware_v5_smoke_receipt.json` seals the manifests and replay.
- 2026-08-31 — materialized the additive readiness-only receipt
  `configs/sft2a/rehearsal_readiness_v5.json` for sample
  `f7d3e27d8361dcbdde245e5236902239b5ca505538a3ca35d5efb80c6e042c4c`, exactly 100 roots and
  400 slots with source mix 42 Mathlib / 25 Physlib / 17 CSLib / 16 safe-context compiler-data.
  It is fail-closed with `authorized: false` under ceilings 2,480 total provider calls, 1,200
  Terra, 1,200 Opus, 80 Kimi, three candidate attempts per slot, and $160 reported Opus spend.
  No rehearsal `tmux` session was started. The approximately-10K gate, 50K run, legacy rejudge,
  and publication remain separately unauthorized.
- 2026-08-31 — received the exact authorization sentence for only the closure-aware v5 rehearsal
  bound to sample `f7d3e27d8361dcbdde245e5236902239b5ca505538a3ca35d5efb80c6e042c4c`,
  config `ba77a49dd162b88e59bdf1fe5cd04687eeaa2affda314dc3b3b0e5cfa2cc16da`, and the
  readiness ceilings. Its exact text hash is
  `d02d8bc031cf01695853e773515214db0ce78926a8494d9ae76ee6bb05a71279`. Materialized the
  additive `configs/sft2a/rehearsal_authorization_v5.json`; the historical readiness-only receipt,
  v5 smoke, and recovery-v4 evidence remain unchanged. This authorization still excludes the
  approximately-10K gate, 50K run, legacy rejudge, publication, and every other run.
- 2026-08-31 — the first v5 detached launch created the requested tmux session but the session
  exited before the worker entered, exactly matching the prior all-descriptors-left-the-PTY startup
  failure. It executed zero provider calls and zero Lean requests and claimed no resource. Preserved
  its two empty artifacts plus manifest under `detached/failed_startup_v1/`. Applied the previously
  proven non-I/O PTY keepalive pattern to the v5 worker at implementation `d0ca454`, added a launch
  journal event, and created additive recovery authorization
  `configs/sft2a/rehearsal_authorization_v5_recovery_v2.json` bound to the same user sentence,
  sample, config, ceilings, and failed-startup seal. No scope was expanded.
- 2026-08-31 — the PTY-safe v5 recovery launch entered the worker, acquired and then released the
  single Lean resource claim, and failed closed while elaborating the first sampled CSLib reference,
  before any Terra, Opus, or Kimi call and before any candidate elaboration. The zero-Lean census
  had omitted an active section variable and retained the `in` modifier from `open Classical in`.
  The historical 24-file failed-run tree is frozen at combined-tree hash
  `3b36a72a61e63e7ae9d9bce6f4262bd6787795f564ce60fff30fb44e04a330fa` in
  `configs/sft2a/rehearsal_v5_failed_launch_seal.json`. The authorization bound to sample
  `f7d3e27d8361dcbdde245e5236902239b5ca505538a3ca35d5efb80c6e042c4c` is retired for relaunch.
- 2026-08-31 — corrected the source-context census conservatively by excluding declarations under
  active section/namespace variables and stripping the trailing `in` open-command modifier. Added
  a two-applicable-mechanism minimum per polarity so both candidate slots have substantive choices.
  The additive v5.1 config has logical hash
  `add8445af25fddc99f3381dbf23d30121847f90c3ef0ddbbba4b09ee8e632f51`; its zero-provider,
  zero-Lean sample is `480a586ea99c26f41c6dfba47fb345507f25bd9bc778c03b13cc22108abd87f5`
  with the unchanged 42/25/17/16 source mix, 12 preserving and 14 breaking mechanism families,
  and 11%/8.5% maximum family shares. A second preparation preserved every durable hash. Readiness
  receipt `configs/sft2a/rehearsal_readiness_v5_1.json` is fail-closed with `authorized: false`,
  output root `runs/rehearsal_closure_aware_v5_1`, session
  `leanfaith-sft2a-v5-rehearsal-v2`, and the unchanged 2,480/1,200/1,200/80/three-attempt/$160
  ceilings. The approximately-10K gate, 50K run, legacy rejudge, publication, and every other run
  remain unauthorized.

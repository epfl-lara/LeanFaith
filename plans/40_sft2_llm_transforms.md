# SFT2A — LLM-generated semantic transformations

> **Task ID:** SFT2A
> **Status:** active
> **Owner/session:** Codex `codex://threads/01a0537f-14af-7e31-9f96-4a7514ccc527`
> **Last updated:** 2026-09-01
> **Dependencies:** REPR `goal_v1.0`; shared rubric; roots may be selected independently of SFT1
> **Next gate:** implement F5--F9, resume the cached row-checkpointed Kimi audit, and pass the
> 20-root performance/resume pilot; a pass automatically launches ten independent 1K-root shards
> **Compute class:** external LLM/API plus CPU/RAM for Lean; large run may need explicit budget approval
> **Lean budget:** compile each novel candidate once through cached persistent workers
> **Local staging root:** `/storage/milikic/leanfaith/value_first/sft2_llm_transforms_v1/`
> **HF destination:** private `Lemmy00/leanfaith-sft2-llm-transforms-v1`

## Active 72-hour sprint override

The active execution contract is
[`72h_sft_data_sprint_2026-09-01.md`](72h_sft_data_sprint_2026-09-01.md). It supersedes the active
sequencing in this older brief while preserving every historical config, receipt, hash, and run as
evidence. Exact authorization sentences, per-failure recovery configs, clean-tree bindings, full
replays, and durable-tree hashing are not dependencies of the sprint path. Do not rerun the
completed 100-root generation.

Keep independent per-slot Terra calls, but decouple provider concurrency from Lean. The blocking
path is only: F5--F9 and focused tests; cached concurrent Kimi-audit completion; a 20-root/80-slot
dynamic-queue pilot with exactly two persistent Lean workers; then automatic reference
certification and ten resumable 1K-root shards. Per-candidate Lean validity, self/gold screening,
accepted-only deduplication, blinded Opus agreement, durable terminals, and invalid-is-not-negative
remain mandatory. Kimi sampling, mechanism agreement, partial replay, and manual inspection run
asynchronously and do not serialize generation.

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

The completed v5.2 recovery-v5 rehearsal measured provider orchestration, especially Terra latency,
as the dominant bottleneck; candidate Lean execution was not the limiting stage. The additive
performance track must therefore decouple provider concurrency from the machine-wide Lean worker
cap: use a dynamic provider work queue with immediate failure reporting while retaining exactly two
persistent, project-grouped Lean workers and their content-addressed cache. Validate this change on
20 roots before considering any larger gate.

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
Create a dedicated SFT2A worktree/branch from local-main coordinator commit
`c17104fe9bec1cb9eaf847c4e412aa0ca76c178a` (or a later coordinator descendant); do not edit the
integration checkout or continue from a pre-integration task tip. Own only SFT2A. Read AGENTS.md,
PLAN.md, plans/00_shared_contracts.md,
plans/72h_sft_data_sprint_2026-09-01.md, and this brief completely. Preserve the completed 100-root
run; do not regenerate it or build more authorization/recovery ceremony.

Implement the minimum sprint path. Persist parseable provider JSON as a terminal even when
semantic-schema-invalid; let the proposer/judge layer retry malformed output once and then route it
to unknown or a slot retry. Keep judge semantics strict and explain error_type/unknown_reason in the
prompt. In the Lean oracle support canonical universes and remaining level metavariables, require
all displayed section variables to be bound, bump the semantic method version, and remove source
file bytes from cache identity. Keep one Terra call per slot. Replace the static two-root schedule
with an as_completed queue: concurrency 8 for the pilot and 16 after it passes, backed by exactly
two locked persistent project-grouped SignatureOracles reused with rebind. Claim global dedup only
after Opus accepts. Make mechanism mismatch telemetry and remove definitional_unfold_refold.
Checkpoint Kimi per row at concurrency 8. Before 10K, stop rereading full ledgers under every lock;
load state once plus append, or use SQLite. Interrupted calls retry under the same semantic key.

First finish the cached 40-row Kimi audit without rerunning generation. Then run exactly 20 unused
certified roots/80 slots and one controlled completed-root resume. Pass only with Lean-invalid
below 25%, accepted slots at least 70%, zero accepted self-pairs/duplicates, no crash on an injected
malformed answer, zero new provider/Lean calls for completed roots, and at most 30 minutes wall
time. A pass automatically certifies about 12K references and launches 10K as ten independent
1K-root tmux shards. Shard 1 starts at concurrency 16 and falls to 8 on sustained throttling;
continue automatically when provider failures stay below 2%, accepted throughput is at least 8
rows/minute, and pilot quality bounds hold. Leave healthy long runs detached with durable journals.
```

## Coordinator requests

- No new exact authorization sentence or model review is required inside the active 72-hour path.
  Objective pilot and shard-1 thresholds authorize the next stage automatically.
- Legacy bulk rejudging, 50K-root expansion, training, and any change to frozen historical evidence
  remain outside this sprint.

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
- 2026-08-31 — received the exact authorization sentence for only corrected v5.1 sample
  `480a586ea99c26f41c6dfba47fb345507f25bd9bc778c03b13cc22108abd87f5` and config
  `add8445af25fddc99f3381dbf23d30121847f90c3ef0ddbbba4b09ee8e632f51` under the frozen
  2,480 total / 1,200 Terra / 1,200 Opus / 80 Kimi / three-attempt / $160 Opus ceilings. Its
  exact text hash is `00a61d1f7c4c9e32069d5d980b2115834fd7baaacd037c9c769dc66abf4cb105`.
  Materialized additive authorization receipt `configs/sft2a/rehearsal_authorization_v5_1.json`
  with hash `5543aeb03a1fc21ed8f71f8fd6988e5822054d9b82b273db59405f03431c26bb`, bound to clean
  readiness commit `e1884ad` and readiness hash
  `1e4b008565908d88a57ae26facbdf9f53e3301dc25e65266ff38b5d8e2352ac7`. The approximately-10K
  gate, 50K run, legacy rejudge, publication, and every other run remain unauthorized.
- 2026-08-31 — launched only `leanfaith-sft2a-v5-rehearsal-v2` through the committed v5.1
  authorization. The worker retained its tmux PTY, acquired the single Lean resource claim, and
  failed closed on the first CSLib reference before any Terra, Opus, or Kimi call. Declaration
  `Cslib.LTS.mem_saturate_image_τ` relies on Lean-generated `autoImplicit` parameters `Label`,
  `State`, and `s` that are absent from its source-text signature; one reference Lean request and
  zero candidate Lean requests executed. The resource was released and the session exited. Froze
  all 21 run files at combined-tree hash
  `043e4d7ef3c63ca3e905d368b85600fce1469da1ad29bcd1d17d9eadfdf5869c` in receipt
  `configs/sft2a/rehearsal_v5_1_failed_launch_seal.json` (file hash
  `3a7c8fc271f7b342df386af0ef46943fa8821f9bda472b9937e1d66854e7b5d6`). V5.1 is retired for
  relaunch. Another text-only filter is not sufficient: the next safe design must certify a larger
  deterministic candidate pool through local Lean with zero provider calls, freeze the 100 valid
  roots only after that certification, and then request a separate exact-sample provider-launch
  authorization. No such new certification or run is authorized yet.
- 2026-08-31 — received authorization for additive v5.2 implementation and only the bounded local
  reference-certification phase. The frozen architecture retrieves imported Mathlib, Physlib, and
  CSLib declarations by qualified name, requires theorem kind, and sends the actual
  `ConstantInfo.type` directly through frozen REPR; only compiler-data uses proof-free term
  elaboration. The initial deterministic pool is fixed at 126/75/51/48 roots, with one optional
  same-source fixed-quota extension block per underfilled source and a hard 600-attempt cap. The
  final certificate must be exactly 42/25/17/16 with a global 100/100 cache-hit preflight before
  any future provider construction. Terra, Opus, Kimi, the provider-backed rehearsal, 10K, 50K,
  legacy rejudging, publication, and training remain unauthorized.
- 2026-08-31 — committed the additive v5.2 implementation at `b555747` (tree `9b4dbec`). The
  zero-Lean pool is frozen at `8ec78fd1823b17565c80ec0ffbf483e1d6907382a30d0500843237d640fdfd9b`:
  its initial and extension blocks each contain exactly 126 Mathlib / 75 Physlib / 51 CSLib / 48
  compiler-data roots, are disjoint, and include `Cslib.LTS.mem_saturate_image_τ` in the initial
  CSLib block. Materialized local-only authorization receipt
  `configs/sft2a/reference_certification_authorization_v5_2.json` against that clean commit; its
  hash is `4d097b49120e2bd6ae6576b9695b378315708d4c6676ff57753293745dc8eef4`.
  The receipt permits zero provider calls and leaves every provider-backed or scale path disabled.
- 2026-08-31 — the first local v5.2 detached launch failed closed after one durable certification
  result and before any provider call. Qualified constant lookup correctly ignored source text, but
  the grouped backend retained the first declaration's context fingerprint while the next pool row
  carried different source namespace/open metadata; the central protocol rejected that mismatch
  before Lean execution. Preserved all 14 files under `runs/reference_certification_v5_2` at
  combined-tree hash `b9a90affabb1a82723bd7a6dd0d17df01a5ed79cd13c319fb40ea2b408b50820`.
  The additive recovery canonicalizes library lookup to one import/options context per project,
  retains source context only in provenance/prompt fields, reuses the shared terminal cache, and
  uses fresh output/session/resource identities. Provider calls remain zero and unauthorized.
- 2026-08-31 — committed the v5.2 recovery at `b25da69` (tree `7602b2f`) and materialized
  `configs/sft2a/reference_certification_authorization_v5_2_recovery_v2.json` with hash
  `e4cbdecb5d33d3f2804aae6644b04417bc2ea61283d4ace441e052c24bf08ba7`. The corrected config
  hash is `4cf6d7f275e98c501e518472ceedde6d88db4166905430672b368a10883a2a8a`; it binds the same exact
  pool `8ec78fd1823b17565c80ec0ffbf483e1d6907382a30d0500843237d640fdfd9b`, fresh output
  `runs/reference_certification_v5_2_recovery_v2`, and session
  `leanfaith-sft2a-v5-reference-certification-v2`. No authorization scope changed.
- 2026-08-31 — recovery-v2 certified all 300 initial roots with no extension and froze an exact
  42/25/17/16 sample, then completed a 100/100 zero-Lean cache replay. Its final global preflight
  failed only because the second replay's durable-tree calculation included the first replay
  receipt, creating an immutable self-reference conflict. Preserved all 327 recovery-v2 files at
  combined-tree hash `f4e90fafd7925887c2ac30fb2ee4def7afa4252470e2875b149fffba28d83958`.
  The recovery-v3 fix excludes derived replay/preflight receipts from their own durable tree and
  has a regression requiring two identical, zero-call replay results. All 300 terminal reference
  cache entries remain reusable; no provider call was made.
- 2026-08-31 — committed replay-stable recovery-v3 at `c23b295` (tree `63c7954`) and materialized
  `configs/sft2a/reference_certification_authorization_v5_2_recovery_v3.json` with hash
  `3a9fc2bf63ee0eccfffb96388ec3cdbb66fc38f531ed69eb3004ba32247b0b52`. Its config hash is
  `28ebd549a1abd85a58796d987b2a56f03f481a3420cd427ff87f0de2b766da15`; it retains pool
  `8ec78fd1823b17565c80ec0ffbf483e1d6907382a30d0500843237d640fdfd9b` and uses fresh output
  `runs/reference_certification_v5_2_recovery_v3` plus session
  `leanfaith-sft2a-v5-reference-certification-v3`. Authorization remains local certification only.
- 2026-08-31 — completed local reference certification and the 100/100 global cache preflight in
  recovery-v3. All 300 initial rows were terminal cache hits in the final replay-stable run, so it
  executed zero Lean requests and zero provider calls; no extension block was needed. The exact
  certified sample hash is `fb2f47f3fae9d8ac584989a2aaec64985a4ad1fa913303714ad267186d0b2bc6`
  with source mix 42 Mathlib / 25 Physlib / 17 CSLib / 16 compiler-data, 100 unique closed-Expr
  hashes, 100 unique rendered-goal hashes, and no gold contamination or placeholder. Manifest hash
  `3bd706899630fb2c9d3dabdda22627242d9f3aa70273309e96ec06953f442be6`; 100/100 preflight hash
  `eadcccc3b8df7a018319d4f71e95b46e50c790006eeae4551a6376b7c97579b5`.
- 2026-08-31 — the fresh local certification measurements behind the shared terminal cache used
  299 Lean requests plus the separately authorized canary, no more than one worker, and peaked at
  7.08 GiB RSS. Per-source durable-event throughput was 7.71 Mathlib, 7.44 Physlib, 8.41 CSLib,
  and 7.51 compiler-data rows/second; the complete detached attempt including project startup and
  compaction was about 90 seconds. The canary succeeded by `loaded_constant_type` and exposed
  `Label`, `State`, `s`, the `HasTau` instance, and `lts` in its certified goal.
- 2026-08-31 — prepared but did not execute the bounded-parallel rehearsal path. Atomic provider
  reservation/finalization, cross-worker deduplication, at-most-two worker claims, mid-root and
  between-root resume, duplicate-launch refusal, deterministic compaction, planned-versus-accepted
  mechanism reporting, and zero-call replay are executable and tested. Repository readiness
  receipt `configs/sft2a/parallel_rehearsal_readiness_v5_2.json` has hash
  `5c0368529d42817c0bb0968e6f43483be0020a031a573001e16dccf34e2b135c`, binds runner commit
  `e9cee4a` (tree `8c77961`), and remains `authorized: false`. All 46 SFT2A unit tests, Ruff
  check/format, strict Mypy, config verification, and diff checks pass. Terra, Opus, Kimi, the
  100-root rehearsal, 10K, 50K, legacy rejudging, publication, and training remain unexecuted and
  unauthorized.
- 2026-08-31 — superseded the readiness-only v5.2 provider receipt without changing any completed
  recovery-v3 certification byte. A model-facing audit found the single rendered ellipsis in
  `Composition.orderEmbOfFin_boundaries`; the corrected additive track rejects `[anonymous]`, the
  Unicode ellipsis, and ASCII ellipses, retains that declaration as a negative regression, and
  deterministically replaces it from the already-certified initial Mathlib pool. The prior
  readiness/config/sample remain immutable historical evidence and are explicitly recorded as
  `superseded_not_authorized`. No provider or new Lean call was made.
- 2026-08-31 — froze corrected certified sample
  `23c80df14d4df72472891d99fb084af8a4cb7644ea173614fd941df11ce5a542` with replacement
  `Filter.bliminf_inf_not` and unchanged source mix 42 Mathlib / 25 Physlib / 17 CSLib / 16
  compiler-data. Applicability now comes from the certified closed Expr plus structured `goal_v1`:
  binders are counted once, nested function arrows are not premises, and lambda `=>` is not an
  equality/order token. All 13 discovered one-binder roots have zero two-binder assignments. The
  100/100 terminal-cache replay and preflight executed zero Lean requests and zero provider calls.
- 2026-08-31 — implemented the fail-closed v5.2 provider path against exactly the corrected
  `certified_sample.jsonl`. Library references are admitted only when their cache key and raw Expr
  payload prove qualified-name `ConstantInfo.type` lookup and all certified Expr/render/sidecar/
  context hashes match; the incomplete source signature is never re-elaborated. Terra, Opus, and
  Kimi share one atomic reserve/call/finalize ledger with reported Opus cost enforcement and
  terminal-cache reconciliation. Root ownership is a validated state machine with explicit crash
  and reclaim transitions, conflict-checked slot checkpoints, owner-only completion, one root per
  worker, deterministic compaction, zero-call replay, audit-disagreement exclusion, and detached
  two-worker/40-GiB launch/resume/health commands. The new authorization materializer is exact-text
  and remains unexecuted; status stays `active` pending a new user sentence.
- 2026-09-01 — the authorized corrected-v4 provider rehearsal stopped fail-closed after 70/100
  completed roots. It finalized 755 cumulative provider calls (545 Terra, 210 Opus, zero Kimi),
  recorded $7.900784 Opus spend, released its resource claim, and left 29 roots unstarted after
  `mathlib:census:1e645cb485bb5184c3149d41` raised `PromptRenderError`. The failed run remains
  immutable at source identity
  `9910915ba060adac26de0630562a7e2a8fa79af58537d79aa82dbf728547d3d1`; its 70 completed
  manifests have seal `734cc83cb066318a7163ec749a42d344b9e89ff3f893325caa6c47570321b985`.
- 2026-09-01 — diagnosed the crash without a provider or Lean call. Frozen REPR legitimately
  rendered the valid candidate fragment `𝓝[{y | y ∉ {x}}] x`; the prompt renderer incorrectly
  treated the adjacent Lean closing braces as an unresolved template token. Template validation
  now separates the frozen template skeleton from interpolated mathematical data, and the exact
  crashed theorem is a regression. Detached failures now persist a fail-closed terminal receipt.
  Additive recovery config `configs/sft2a/provider_rehearsal_v5_2_recovery_v5.json` has hash
  `5130e73b58177205b16faa315b3694f913f50e0ec0de2acc40a017b2924d7269`, uses fresh output/session/
  resource identities, and exact-copies the 755-call ledger before authorization so calls and spend
  cannot reset across recovery. All 53 SFT2A unit tests, scoped Ruff check/format, strict Mypy,
  config verification, and diff checks pass with zero provider calls and zero Lean requests. The
  recovery remains unauthorized; 10K, 50K, legacy rejudging, publication, and training remain
  unauthorized.
- 2026-09-01 — the authorized recovery-v5 generation completed all 100 certified roots and all 400
  planned slots, producing 284 accepted core rows (126 preserving and 158 breaking). Deterministic
  replay covered all 100 roots with zero provider calls and zero Lean requests while preserving the
  compacted artifacts. This completed generation is durable evidence and must not be rerun merely
  to repair or complete the downstream audit.
- 2026-09-01 — the detached job then failed closed during the Kimi audit, after generation and
  replay, because a binary `non_equivalent` verdict carried a non-`none` structured `error_type`.
  No completed Kimi audit manifest exists, and the shared provider ledger retains one outstanding
  reservation. No SFT2A process, tmux session, or resource claim is currently active. The measured
  bottleneck was serialized provider orchestration rather than Lean. SFT2A therefore remains
  `active`: first complete an additive, per-row-checkpointed Kimi audit repair over the existing
  generated rows; then pass a bounded 20-root dynamic-concurrency pilot with exactly two persistent
  Lean workers. Approximately 10K, 50K, legacy rejudging, publication, training, and any generation
  rerun remain blocked.
- 2026-09-01 — the coordinator performance postmortem counted 767 Terra calls with approximately
  20,151 aggregate provider-seconds, 303 Opus calls with approximately 3,660 aggregate
  provider-seconds, and only 165 executed candidate-Lean requests totaling approximately 44.6
  seconds. The current path statically assigns roots to two workers, waits on futures in submission
  order, closes/recreates the Lean oracle per root, serializes all slot attempts, and writes the
  Kimi audit only after the full sequential loop. In contrast, the Numina runner keeps a dynamic
  `as_completed` queue over independent provider calls and appends resumable results. The additive
  performance pilot must adopt that scheduling pattern for provider work, checkpoint the audit per
  row, and keep only the measured two persistent Lean workers; it must not relax semantic checks.
- 2026-09-01 — adopted the code-grounded GPT Pro/Fable 5 review through the active 72-hour sprint
  override. Historical authorization/recovery/full-replay sequencing remains immutable evidence,
  not an active dependency. The task now fixes F5--F9, completes the cached Kimi audit, runs one
  20-root performance/resume pilot, and automatically proceeds to ten 1K-root shards on the stated
  thresholds. No provider or Lean call was made by this plan update.
- 2026-09-01 — Kilo session created the `milikic/sft2a-72h-sprint` worktree from local-main
  `5de43eb` and cherry-picked only the F5/strict-judge code+test checkpoint (`fd5e76e` ->
  `9dc463f`). F5 persists schema-invalid provider output as an immutable terminal and routes the
  proposer/closure-judge/v1-judge paths to retry/unknown instead of crashing. F6 rejects binary
  verdicts with `confidence=low`. All 58 SFT2A tests pass; Ruff and strict Mypy clean. The stale
  `42cd0d6` brief archival checkpoint (`c7f6178`) was intentionally not cherry-picked. Began the
  cohesive F7--F9 + Kimi-audit-resume + 20-root-pilot track on this worktree.

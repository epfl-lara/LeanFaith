# SFT2B — autoformalization consistency data

> **Task ID:** SFT2B
> **Status:** waiting_user
> **Owner/session:** Codex `/root` — 2026-08-30 SFT2B session
> **Last updated:** 2026-08-30
> **Dependencies:** REPR `goal_v1.0`; source-quality audit and frozen consistency/voting prompts
> **Next gate:** obtain one A100-80GB or H100-80GB placement for the pinned ReForm-32B one-source
> command, measure it, and get an explicit compute/model decision before any matched 500-source run
> **Compute class:** model inference GPU plus Lean CPU; 32B pilot likely needs A100/H100
> **Lean budget:** compile each novel formalization candidate once through persistent cached workers
> **Local staging root:** `/storage/milikic/leanfaith/value_first/sft2_autoformalizer_v1/`
> **HF destination:** private `Lemmy00/leanfaith-sft2-autoformalizer-v1`

## Objective and scale

Build a 50K-source, diverse natural-language-to-Lean consistency corpus. Each source must have
standalone natural language and a trusted Lean reference theorem. Generate four Lean candidates per
source with ReForm or a better pilot winner, compile them, and ask Codex, Lemex, and Claude whether
each candidate preserves the same intended mathematical claim as the reference.

## Source-quality work comes first

Do not default to all eligible `sft_classic_numina` rows. Audit and freeze a varied source mix with
trusted NL↔Lean alignment, standalone NL quality, theorem-only extraction, compilation context,
domain diversity, duplicate/benchmark overlap, and lightweight provenance/license fields.

Initial target mix:

- 35% Mathlib, Physlib, and CSLib declarations with linked explanatory docstrings;
- 35% carefully sampled theorem/problem datasets, including strong
  `formalmathatepfl/sft_classic_numina` rows without letting one competition style dominate;
- 20% broader public/synthetic NL↔Lean data such as Lean Workbook candidates after quality audit;
- 10% specialist/high-difficulty sources discovered by this task.

Record source URL/repository, revision, license-card value, redistribution note, NL extraction rule,
trusted-reference basis, and contamination flags. Research use permits a lightweight audit, but
unknown/restrictive redistribution stays private and explicitly tagged.

External-model processing of `formalmathatepfl/*`, including selected `sft_classic_numina` rows, is
authorized by `policies/source_use_v2.yaml`. Create additive source configs under `configs/sft2b/`
with `policy_version: source_use_v2` and `external_transmission: true`; never edit v1 private-source
configs or their hash-bound historical executors.

### Seeds and exclusions

- Reuse the 301 compiled Mathlib docstring/generated-Lean records under
  `data/raw/real_outputs/gate3_docstrings_operational_v1/` as the first end-to-end pilot after
  DATA-REUSE confirms paths/hashes.
- ConsistencyCheck contributes 570 human-accepted examples as a small seed/representation study;
  its 289 diagnostic false rows do not supply trusted NL↔Lean references. It is not the core
  Lean↔Lean evaluation dataset.
- ShadowBench is 126-row, test-only and lacks a trusted Lean reference. Reserve it as a separate
  `reference_free_challenge` external autoformalization benchmark. Do not train on it or describe
  its outputs as reference-pair equivalence data.

## Candidate generation and labels

Pilot `GuoxinChen/ReForm-8B` versus `GuoxinChen/ReForm-32B` on the same 500 sources, four candidates
per source, with identical decoding/seed accounting. The pilot may identify another formalizer, but
must record comparable compilation, diversity, faithfulness-vote, throughput, and resource cost.
Request A100/H100 placement before running the 32B model if the local GPU cannot support it.

For every candidate:

1. apply cheap structural/placeholder checks;
2. compile once in the correct project/import context;
3. for valid candidates, render reference and candidate as pinned `goal_v1.0`;
4. ask Codex, Lemex, and Claude independently under the shared consistency rubric;
5. label `true` for at least two equivalent votes, `false` for at least two non-equivalent votes,
   and unknown otherwise.

Both valid equivalent and valid non-equivalent pairs belong in the core `{reference, candidate,
label}` view. Invalid candidates belong in a separate validity/attempt view, not label `false`.
Preserve structured votes, rationales, confidence, relation/error class, NL, model generation data,
and compilation evidence in a keyed rich sidecar for possible SFT2 auxiliary heads.

## Scope and ownership

**In scope:** source discovery/audit, deterministic sampling, trusted-reference extraction,
formalizer pilot, candidate compilation, three-judge voting, existing-pilot reuse, manifests,
configurations, and private publication.

**Out of scope:** using test-only ShadowBench for training, treating compilation as equivalence,
turning invalid Lean into a semantic negative, exhaustive legal review, or full model training.

**Writable paths:** this brief; `src/leanfaith/sft2b/`; `configs/sft2b/`; `prompts/sft2b/`;
`tests/unit/sft2b/`; the staging root. Existing collect2/source/provider/generation modules, shared
Lean/project/dependency paths, and other tasks are read-only interfaces; request coordinator
changes.

**Paths claimed by this session:** `plans/50_sft2_autoformalizer.md`;
`src/leanfaith/sft2b/`; `configs/sft2b/`; `prompts/sft2b/`; `tests/unit/sft2b/`;
`/storage/milikic/leanfaith/value_first/sft2_autoformalizer_v1/`. All currently modified or
untracked paths outside this list are unrelated work and remain untouched.

## Pinned REPR dependency

SFT2B consumes only the fourth `goal_v1.0` freeze and refuses to start a process when any pin
differs:

- coherent freeze commit `176a783842c5a73b84413dfa8347670608b615d9`;
- canonical spec hash `68d893a2c566bf3f6a82c899a32a351f9a5420f5ea98168c99b887aaa01a45a8`;
- implementation-set hash `9a9252fff5ffc69cb65e71120fedffa83ed47271aecadbecf0ceb890feea65ff`;
- commit-bound renderer API hash
  `c695ad868c98f27218e82184559d90624491df25c7805bf29861dd891787261d`.

The SFT2B runtime reads the frozen REPR config directly and verifies every remaining renderer,
Python, Lean, injected-helper, universe-profile, render-context, and config identity declared there
against live files before creating a journal, Lean request, judge call, or formalizer request. The
SFT2B config records those derived pins for audit, but the frozen REPR config is authoritative; a
copied partial hash list is not accepted.

## Executable subplan

1. **Lean-free dependency and reuse gate.** Reconcile the DATA-REUSE manifest, evidence, and adapter
   previews with the canonical 301-record source tree. Freeze exact source/catalog paths, revisions,
   file or tree hashes, join keys, compile contexts, and the three-public/195-Algebra/103-cross-domain
   count split. Treat all 301 labels as unknown three-voter inputs and preserve the superseded public
   tranche exclusion. Verify the REPR pins above and all derived frozen-config pins at process
   startup.
2. **Versioned records before execution.** Define strict schemas for source records, exactly four
   deterministic candidate slots, formalizer lineage, raw proof-free signatures, compile/elaboration
   evidence, REPR sidecars, three structured blinded votes, majority outcome, minimal core rows,
   invalid attempts, unknowns, journal events, and manifests. Stable IDs derive from canonical source
   identity, slot, candidate/context/toolchain/helper versions, and never row position. Deterministic
   compaction rejects duplicate terminal events.
3. **Frozen independent judging.** Check in separate Codex, Lemex, and Claude prompt/config records
   that state the shared intended-claim consistency rubric directly. Each judge receives the same
   cached reference/candidate `goal_v1.0` pair and source NL, but no expected label, sibling vote,
   majority, or other judge rationale. Parse only the frozen structured response schema. Invalid Lean
   does not reach semantic voting; ambiguity or insufficient confidence routes to `unknown`.
4. **One-elaboration trusted renderer helper.** Build a task-owned, content-hash-pinned client behind
   `leanfaith.lean.protocol`. For a proof-free theorem signature, it elaborates the proposition once
   in its exact project/import/namespace/scope/options context and passes that same live closed
   `Expr` directly to `LeanFaith.GoalV1.renderClosedProp`. It never inserts `sorry`, creates a proof or
   axiom, guesses from surface text, or pretty-prints and re-elaborates. One initialized LeanInteract
   environment serves a context batch. Cache trusted references once per source and candidates once
   per `(candidate, context, toolchain, helper, REPR implementation)` identity; the three judges share
   the cached result. Reject model-facing renders containing `[anonymous]` or `⋯`.
5. **Exact one-existing-candidate smoke.** Select one canonical DATA-REUSE preview and trace source
   recovery, cached reference/candidate elaboration, direct-Expr rendering, three real blinded votes,
   majority or unknown routing, core/invalid/unknown sidecars, append-only journal, deterministic
   manifest, and a restart. Prove the restart emits no duplicate Lean, formalizer, or judge call and
   does not modify the legacy source. Report serialized rows, hashes, cache keys/hits, call counts,
   startup/row latency, failure class, and peak RAM.
6. **One-source ReForm-8B generation smoke.** Only after the existing path passes, claim the one local
   GPU and measured Lean budget, generate four recorded slots for one new audited source, and run the
   same cheap-check/compile/render/vote/route path. Record VRAM/RAM, timing, candidate diversity,
   validity, and restart evidence. Prepare, but do not run locally, the equivalent hash-pinned
   ReForm-32B command; request A100/H100 placement.
7. **Stop for the matched pilot decision.** After both one-source paths are sound, propose the same
   frozen 500 sources, four slots, decoding, seeds, contexts, and metrics for 8B versus 32B. Include
   measured/projected hardware, duration, storage, judge calls and cost, journals, checkpoint/resume
   procedure, and the source-quality audit plan. Do not launch the comparison, 50K generation,
   publication, or training without user compute/model approval.

Lean is the bottleneck throughout this plan: all safe string parsing, source filtering, schemas,
provenance, joins, deduplication, prompt validation, hashing, and restart logic precede Lean. The
only initial Lean work is the bounded one-record oracle after a shared resource claim. Persistent
workers and synchronous elaboration replace per-row processes; deterministic failures are cached,
only infrastructure failures are retried, and larger 1/100/about-10K measurements require their
preceding gate and coordinated host budget.

## Lean-efficiency plan

Lean is the bottleneck. Compile each generated candidate exactly once after cheap rejection. Group
by project/toolchain/import context, prepare persistent worker pools once, batch requests, and cache
terminal verdicts plus rendered goals. Share cache results across all three judges and reruns. Do
not recompile the trusted reference per candidate; render/cache it once per source. Retry only
infrastructure failures and coordinate total Lean workers across concurrent tasks.

## Execution gates

### One-example existing-pilot smoke

Trace one of the 301 records from NL/reference/candidate through cached compilation, goal rendering,
three blinded votes, label/unknown routing, core or auxiliary row, manifest, and resume without
duplicate model/checker calls.

### Source freeze

Audit at least 100 rows per proposed source class. Freeze inclusion rules, target proportions,
dedup/contamination rules, revisions, and quality examples. Report shortages rather than filling
them with low-quality rows.

### Formalizer pilot

Compare 8B/32B (or justified alternative) on the same 500-source set. Report compilation rate,
unique candidates, positive/negative/unknown vote outcomes, domain breakdown, Lean/model
throughput, VRAM/RAM, cost, and projected 50K-source time. Get user compute/model approval.

### Matched 500-source pilot proposal — not authorized

Freeze exactly 500 source IDs only after auditing at least 100 candidates from each proposed source
class. Target 175 Mathlib/Physlib/CSLib docstring sources, 175 audited theorem/problem rows, 100
audited broader public/synthetic rows, and 50 specialist/high-difficulty rows; report a shortage
rather than backfill from low-quality `sft_classic_numina`. Exclude ShadowBench and all frozen
benchmark exact/near hits. Both formalizer arms receive the same ordered source IDs, exact project
contexts, four slots, seeds 0--3, prompt/extractor, and decoding settings. Compaction compares
attempt rate, valid rate, unique candidate rate, true/false/unknown votes, domain coverage,
throughput, and resources.

Current planning envelope, based on the accepted one-source 8B smoke rather than an extrapolated
corpus compile:

- ReForm-8B uses one local RTX 4090. Four sequential slots took 198.277 seconds total and peaked at
  16.944 GB allocated / 17.268 GB reserved VRAM, projecting 27.54 GPU-hours for 500 sources before
  startup amortization or failures.
- ReForm-32B uses exactly one A100-80GB or H100-80GB. The pinned BF16 snapshot is 65,540,277,627
  bytes and is forbidden on the local 24-GB GPU. Its 500-source duration remains deliberately
  unquoted until the requested one-source placement supplies measured tokens/second, VRAM, and
  wall time; the pilot duration is then `500 * measured_one_source_seconds` with the same four
  slots.
- Lean uses one persistent synchronous worker grouped by project/context, never a process per
  candidate. The accepted batch rendered one trusted reference plus three candidates in 9.840
  seconds at 3,003,629,568 bytes peak RSS. A deliberately conservative non-amortized projection is
  1.37 worker-hours per 500-source arm; content-addressed terminal failures and successes are reused.
- The observed smoke admitted two of four attempts and made six judge calls in 91.12 seconds. At
  that admission rate each arm projects 3,000 calls and 12.66 sequential judge-hours; the hard
  ceiling is 6,000 calls and 25.31 hours per arm when all 2,000 candidates are valid. The CLIs did
  not expose billable tokens or monetary charges, so the auditable monetary budget is the formula
  `2000 * (Codex pair cost + Lemex pair cost + Claude pair cost)` per arm, with an expected 0.5
  multiplier from this single smoke. A billing export or approved per-provider pair price is a
  launch prerequisite; no dollar value will be invented.
- Durable outputs used 315,454 logical bytes for the accepted source including generation and the
  smoke root; two 500-source arms project about 315 MB. Reserve 1 GB for journals, raw responses,
  caches, and atomic compaction, plus 16.397 GB for the existing 8B snapshot and 65.540 GB for 32B;
  request at least 100 GB working storage.

Each `(source, arm, slot)` has a stable ID and append-only terminal event. Completed generation,
endpoint, and vote cache entries are immutable; deterministic compaction writes atomic outputs and
restarts from the last terminal event without provider or Lean duplication. The 32B one-source
measurement and the monetary judge-cost input must be reported before the user can authorize this
proposal.

### Scale and publication

Use source/candidate-slot journals, stable IDs, atomic shards, and deterministic compaction.
Publish core pairs, rich votes, invalid attempts, source/NL records, and reference-free challenge as
clearly separate configurations/sidecars. Screen both signature sides against
`data/benchmarks/golden_blocklist_v1.json` with exact and near-duplicate keys and exclude hits from
training. Verify remote counts/schema/hashes and Hub revision.

## Acceptance criteria

- Every core source has standalone NL and a trusted Lean reference with pinned provenance.
- Source mix is varied and quality-audited; no blind dependence on `sft_classic_numina`.
- Four candidates/source are attempted with a recorded formalizer decision.
- Valid equivalents and non-equivalents are both retained; invalid and unknown are separate.
- Three independent votes are stored and majority labels follow the frozen rubric exactly.
- Lean/model work is cached, resumable, measured, and compute-approved before scale.

## Session kickoff prompt

```text
Own only SFT2B in /localhome/milikic/LeanFaith. Read AGENTS.md, PLAN.md,
plans/00_shared_contracts.md, and plans/50_sft2_autoformalizer.md completely. Update this brief and
claim exact paths. Start with a source-quality audit and one of the existing 301 candidates through
the complete compile-and-three-vote loop. Do not assume sft_classic_numina is sufficient, and keep
ShadowBench test-only/reference-free. Lean is the bottleneck: compile each candidate once in a
persistent cached pool and share that evidence. Keep valid false pairs in core; invalid and unknown
are separate. Compare ReForm 8B/32B on 500 sources and request A100/H100 before unsupported runs.
Do not launch 50K sources without the pilot and user compute/model approval.
```

## Coordinator requests

- Approve source freeze and formalizer/hardware choice after the 500-source pilot.

## Progress log (append-only)

- 2026-08-30 — task brief created; no source selection, inference, compilation, or voting run.
- 2026-08-30 — Codex `/root` claimed only the SFT2B brief, task package/config/prompt/test paths, and
  staging root. Recorded the fourth REPR freeze pins and the ordered executable subplan before any
  substantial execution. Initial work is Lean-free DATA-REUSE reconciliation plus strict schemas,
  prompts, hashes, journals, and restart logic; no Lean, model, judge, upload, or training call has
  run. Lean remains the bottleneck, so the first Lean action will be one resource-claimed record in
  one persistent context, never a corpus compile or per-row process.
- 2026-08-30 — completed the Lean-free dependency gate. The canonical DATA-REUSE recipe resolves 17
  manifests to exactly 301 unknown-label candidates and 50 references: 3 public-research, 195
  Algebra, and 103 cross-domain. A task-owned receipt binds all 1,828 files actually consumed
  (manifests, six invocation artifacts per candidate, reference catalogs, and contexts) at
  `42c2501bc17daed82594e4be84150e3b27011204b2aff7ad56d130812d97c2dc`; the accepted DATA-REUSE
  tree hash alone did not cover every admitted-candidate file, so it is retained as provenance but
  not used as the sole runtime integrity boundary. Startup verification replays the REPR config,
  renderer/helper/Python sources, semantic/universe/context/coverage hashes, implementation set,
  API hash, task helper, judge schema/prompts, binaries, and versions before staging or calls.
  Added strict records, independent blinded judge clients, the one-elaboration live-Expr helper,
  content-addressed caches, append-only journal, deterministic routing, and atomic restart terminal.
  Cheap verification passes: 11 SFT2B unit tests, Ruff, and strict mypy. No Lean or model call has
  run because SFT1 currently owns the full shared two-worker/40-GiB Lean budget; SFT2B is still
  `active`, not blocked, and will claim one worker only after that reservation releases.
- 2026-08-30 — passed the exact existing-301 end-to-end gate with public-research pair
  `pair:e899befb44b83b09dd0f82777d48ea44ec3efac642b85650e0040a4f0e2fcf29`.
  The accepted run is
  `/storage/milikic/leanfaith/value_first/sft2_autoformalizer_v1/smokes/sft2b_run:be964d32a72bd578cf5c6ddc81844fc48b1b280ff92b79c659ffd2d38ad011eb`:
  one persistent Lean request elaborated and rendered the reference and candidate in 8,288 ms;
  three blinded Codex/Lemex/Claude calls all voted equivalent; compaction emitted exactly one
  `{reference,candidate,label}` core row with label `true` and SHA-256
  `6d1e12a20b0384d0aad571731b625d5bd00f75595b15157d6475656cf38f05b7`.
  Immediate replay made zero Lean and zero judge calls, and a separate-process replay resumed the
  verified manifest. Two earlier immutable attempts preserve a helper-API invalid route and a
  provider-schema incompatibility; neither was relabeled semantic `false`.
- 2026-08-30 — exercised ReForm-8B source/output-contract failure paths without hiding them. The
  first audited Mathlib docstring run used four model calls and produced zero extractable candidates
  because ReForm emitted its native reflection-plus-theorem format. A versioned native-declaration
  extractor now discards the declaration name and placeholder proof, explicitly generalizes
  theorem-style `Type*` universes, and sends only a proof-free proposition to Lean. On a second
  absent-from-301 Mathlib source this extracted two candidates, both with the wrong `Ideal.span`
  API; the other two slots remain formalizer-invalid auxiliaries. These generation roots are
  immutable evidence, not pilot inputs or negatives.
- 2026-08-30 — selected exactly one competition-style operational smoke row rather than defaulting
  to `sft_classic_numina`: train UUID `5e9411f4-450c-5cf1-a3d8-d3a87a4aaa6a` at pinned dataset
  revision `b3e537486452a88406507c4c2d6f347d46077f61`, train parquet SHA-256
  `99aca3cb596e8de32274f611db72835b7222ba4d15ffbbcb32cafde66e4e5e80`.
  Its exact row/question/Lean hashes, `valid=true`, question/reference agreement, source-use-v2
  authorization, existing-301 absence, and frozen golden exact/near screens replay before use; it
  remains `training_eligible=false`. ReForm-8B generation root
  `/storage/milikic/leanfaith/value_first/sft2_autoformalizer_v1/generation/reform_8b/sft2b_generation_run:dbbe08b7d7667e55040e8df089b704551e5dd60fbe121d82192cafcae7b993ce`
  contains four durable seeds: three proof-free candidates and one output-contract invalid. The
  measured attempts took 25.4--75.4 seconds and peaked at 16.6--16.9 GB allocated / 17.0--17.3 GB
  reserved VRAM. No Lean or judge call for these candidates has run because SFT1 claimed the full
  shared two-worker/40-GiB budget immediately afterward; SFT2B remains `active` and will not
  oversubscribe it.
- 2026-08-30 — prepared but did not run ReForm-32B. Placement config
  `configs/sft2b/reform_32b_placement_v1.json` pins upstream commit
  `80e9d9d83998d8c118c512bd6a35d1cdf11b57c8`, all 26 file sizes and Git/LFS content identities,
  the same one-source config, four seeds, prompt, output extractor, and decoding. The task-owned
  worker verifies every downloaded byte and refuses less than 80,000,000,000 bytes of VRAM. The
  requested placement is one A100-80GB or H100-80GB; the local 24-GB GPU is explicitly forbidden.
  No 32B download or inference was launched.
- 2026-08-30 — completed the accepted new-source ReForm-8B path at
  `/storage/milikic/leanfaith/value_first/sft2_autoformalizer_v1/smokes/sft2b_run:09db83a8b13421291dc2d8ba839686c796716345bca33be8d65abf352e8090dc`.
  Generation supplied four durable attempts: one output-contract invalid and three proof-free
  propositions. One persistent LeanInteract request elaborated the trusted reference and each
  candidate exactly once in 9,840 ms at 3,003,629,568 bytes peak RSS. Two candidates were valid;
  slot 2 failed elaboration and was routed only to keyed validity data, never semantic label
  `false`. The six blinded Codex/Lemex/Claude votes were all equivalent, yielding two exact
  `{reference,candidate,label}` core rows, no unknowns, and core SHA-256
  `496ac8722b8b3a82fd482adb810a5b285cbccbd3e3aad5b8258f803fe49a5a96`.
  Manifest SHA-256 is `e6a0178aaa5b629bebaff43d355c85cca6d75175d3ebb651af559b2437870a09`;
  all four REPR freeze pins and derived config/helper/context hashes replayed before the call, no
  model-facing render contained `[anonymous]` or `⋯`, and all host resources were released.
- 2026-08-30 — verified restart behavior twice. The in-process restart reported zero formalizer,
  Lean, and judge calls, and a fresh process resumed the same manifest with
  `restart_formalizer_calls=0`, `restart_lean_request_count=0`, and
  `restart_judge_call_count=0`. Frozen outputs include two valid core rows, one separate invalid
  attempt, six structured blinded votes, an empty unknown view, content-addressed endpoint/vote
  caches, the append-only journal, raw Lean response, and deterministic hashes. Narrow verification
  passes: 20 SFT2B unit tests, Ruff on all task-owned Python/tests, and strict mypy on 17 source
  files. No 32B inference, matched 500-source comparison, 50K generation, publication, or training
  ran. Status is `waiting_user` for the A100/H100 one-source placement and subsequent explicit
  compute/model decision.
- 2026-08-30 — user authorized a private testing-subset publication before moving to another
  machine. Published exactly the two accepted smokes to private dataset
  `Lemmy00/leanfaith-sft2-autoformalizer-v1` at pinned Hub revision
  `878b3cab22883c732f05a5c30a9119d143e62489`. Release
  `sft2b_hf_subset:c90f9650dd0a247ac54db8b48ce3bb5c6db41b59caa5e664fe31213935c37983`
  contains separate one-row existing-301 and two-row ReForm-8B core configurations, 2 source rows,
  4 candidates, 4 compilation records, 9 blinded votes, 3 majority outcomes, 1 Lean-invalid
  auxiliary, and 1 output-contract-invalid auxiliary. It also carries the accepted source
  manifests and a checksum-bound `repro/workspace/` snapshot of every SFT2B-owned code, config,
  prompt, test, and brief file needed to reconstruct this uncommitted session state. The 77 staged
  files total 468,932 bytes; release-manifest SHA-256 is
  `418d7cbad60dc1783ce1b68b91111876a7155d20f4fdae811068223d0011cef0`.
  Verification downloaded that exact private revision into a fresh directory, matched the staged
  file set apart from Hub-generated `.gitattributes`, passed every `SHA256SUMS` entry, and loaded all
  10 advertised configurations remotely at the expected counts. Local publication receipt:
  `/storage/milikic/leanfaith/value_first/sft2_autoformalizer_v1/hf_subset_v1/publication_receipt.json`.
  This subset publication does not authorize the 32B smoke, matched 500-source pilot, 50K release,
  or training; status remains `waiting_user` for A100/H100 placement and a later compute decision.

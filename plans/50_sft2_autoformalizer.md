# SFT2B — autoformalization consistency data

> **Task ID:** SFT2B
> **Status:** waiting_user
> **Owner/session:** Codex `/root` — 2026-09-01 additive Opus/Terra source-review contract v4
> **Last updated:** 2026-09-01
> **Dependencies:** REPR `goal_v1.0`; source-quality audit and frozen consistency/voting prompts
> **Next gate:** preserve the passing one-row Opus/Terra evidence and await an explicit decision on
> whether the remaining 991 rows may be reviewed under this model-panel contract. Before any such
> calls, freeze an additive full-packet executor/config and its concurrency, time, token, and cost
> ceilings. Separately ingest genuine missing matched-pilot shutdown/resource/zero-call-replay
> receipts if they exist. Do not infer or request generation-scale authorization.
> **Compute class:** source-freeze work is Lean/GPU-free; downstream inference uses eight
> A100-SXM4-80GB GPUs at DP=4/TP=2
> **Lean budget:** compile each novel formalization candidate once through persistent cached workers
> **Local staging root:** `/scratch/milikic/data/leanfaith/value_first/sft2_autoformalizer_v1/`
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

### Authorized matched-500 input-freeze subplan

This session owns only source-freeze preparation and additive private publication. It does not run
ReForm-8B/32B, Lean, any judge, output publication, or training.

1. Replay the frozen source-use policy, DATA-REUSE evidence, source catalogs, exact and signature-
   near golden blocklists, stable-ID rules, and ShadowBench exclusion without invoking Lean.
2. Audit and deterministically select exactly 500 standalone-NL/trusted-reference `SourceRecord`
   rows at the already-proposed 175 library-docstring / 175 theorem-problem / 100 broader-public /
   50 specialist mix. Preserve exact project, toolchain, imports, namespaces/scopes/options, source
   revisions, licenses/policies, trusted-reference basis, and selection/exclusion evidence.
3. Reuse the four frozen candidate slots and seeds from `reform_32b_placement_v1.json`. Render the
   exact pinned ReForm input template for every row, tokenize it with the exact pinned ReForm
   tokenizer revision, record each prompt-token count, and freeze the smallest safe model context as
   `max(prompt_tokens) + 4096`.
4. Deterministically emit `sources.jsonl`, `prompt_token_counts.json`, `source_manifest.json`, and
   `SHA256SUMS` under the task staging root. Validate strict schemas, stable-ID uniqueness, source
   counts/mix, provenance, authorization, deduplication, contamination, context completeness, prompt
   hashes, tokenizer hashes, placement-config hash, REPR pins, and byte checksums.
5. Copy/download into a fresh directory, rerun every validation from those bytes, then upload only
   `pilot_inputs/reform_matched_500_v1/` additively to private dataset
   `Lemmy00/leanfaith-sft2-autoformalizer-v1`. Verify the immutable Hub revision by a second fresh
   download and checksum comparison. Do not overwrite or rewrite revision
   `878b3cab22883c732f05a5c30a9119d143e62489` or any existing remote path.
6. Commit and push only this brief plus task-owned source-freeze code/config/tests. Hand off exact
   Git/HF revisions, four remote paths and hashes, source mix, token maximum and required
   `max_model_len`, slot IDs/seeds, REPR/tokenizer/prompt/config hashes, and fresh-download result.

Lean is the bottleneck: all work in this source-freeze session is deliberately Lean-free. Existing
trusted compilation evidence and exact contexts are replayed; no corpus compile or per-row process
is started.

### Authorized matched-500 one-command runner subplan

This session may implement and test orchestration but does not launch the 2,000-request GPU run on
the original machine.

1. Fetch the exact private input revision and four-file path, verify its checksums, strict 500-row
   `SourceRecord` schema, source ordering, mix, prompt hashes/counts, prompt/tokenizer/placement and
   REPR pins, four slots/seeds, and required model length before any model download or server start.
2. Download the exact ReForm-32B revision, replay every pinned Git/LFS file identity, require eight
   A100/H100-class GPUs, and start the existing vLLM backend at DP=4/TP=2 with the measured profile.
3. Generate the Cartesian product of the 500 ordered source IDs and four frozen slots with
   content-addressed terminals, append-only journals, bounded concurrency, resume, and duplicate
   suppression. Shut the server down cleanly on success or failure.
4. Deterministically compact raw responses, formalizer attempts, admitted candidates, invalid
   output-contract attempts, telemetry, and a manifest. Do not run Lean or judges and do not create
   semantic labels in this command.
5. Upload the compacted generation outputs additively under a new content-addressed Hub path, obtain
   the immutable revision, fresh-download it, replay checksums/counts/IDs, and emit a machine-readable
   receipt. Never overwrite the frozen input path or the earlier two-source release.

Lean is the bottleneck for the later compilation stage, so this generation-only command invokes no
Lean process. It performs every schema, string, provenance, hash, join, and deduplication check
before GPU startup.

### Authorized diverse full-source freeze subplan

The user explicitly authorized preparation of the full SFT2B source set while the separate 8xA100
host runs the matched-500 generation. This subplan prepares and privately publishes source inputs
only; it does not launch ReForm, Lean, judges, semantic labels, generated-output publication, or
training.

1. Inventory pinned local and Hugging Face evidence for every plausible trusted NL-to-Lean source,
   including Mathlib, Physlib, CSLib, the user's curated `lean-docs`/library snapshots, Numina,
   Lean-Workbook, and other already-present public datasets. Record exact revisions, file hashes,
   licenses/redistribution notes, source-use authorization, project/toolchain/import contexts, and
   whether each family actually supplies standalone NL linked to a theorem. Lean-only CPT text is
   not silently promoted into SFT2B when no trustworthy NL alignment exists.
2. Reuse the strict versioned `SourceRecord` contract and stable IDs. Library rows require an
   attached explanatory docstring/comment plus a proof-bearing theorem declaration in a pinned
   successful library snapshot; dataset rows require a pinned standalone problem statement and an
   exact trusted Lean reference with auditable success evidence. Reject proof leakage, prompt text,
   placeholders, malformed/ambiguous declarations, and incomplete compilation contexts without
   invoking Lean.
3. Audit each family before setting quotas. Target roughly 50K rows with deliberate domain and
   ecosystem diversity rather than Numina-only backfill; publish the maximum quality-qualified set
   if fewer than 50K survive. Preserve Mathlib/Physlib/CSLib, competition/problem, broader
   public/synthetic, and specialist/high-difficulty family counts separately.
4. Replay source-use policy, the existing-301 exclusion, ShadowBench/test-only exclusion, golden
   exact/near/problem-identity screens, global NL/reference/signature-near deduplication, and
   cross-family priority rules. Record every audited, eligible, selected, and exclusion count.
5. Measure one row, 100 rows, then about 10K rows before full selection. All scans, parsing,
   provenance, joins, hashing, deduplication, contamination screening, prompt rendering, and token
   counting are Lean-free. Any genuinely necessary Lean oracle remains separately bounded and
   resource-claimed; no process-per-row validation is permitted.
6. Render the exact pinned ReForm prompt for every selected row and record prompt hashes/token
   counts with the frozen tokenizer revision. Emit strict source, prompt-token, family-audit,
   exclusion, manifest, and checksum files with deterministic compaction and a durable terminal
   marker. If the full build outlives the interactive turn, run it in a named detached `tmux`
   session with committed code/config, persistent log/journal, ceilings, resume command, and the
   required startup health evidence.
7. Validate from a fresh directory, then upload additively under a new content-addressed path in
   private `Lemmy00/leanfaith-sft2-autoformalizer-v1`; never overwrite the matched-500 input or any
   prior release. Fresh-download the immutable Hub revision and replay schemas, counts, IDs,
   checksums, source mix, context completeness, prompt tokens, provenance, and contamination.
8. Commit and push only the SFT2B brief plus task-owned source-freeze code/config/tests. Hand off
   exact Git/HF revisions, source-family and domain counts, shortages/exclusions, licenses/policies,
   maximum prompt length, required model length, hashes, cache/output roots, and the generation
   consumer command.

Lean remains the bottleneck: this source-only preparation deliberately completes every cheap
operation before any future candidate compilation, uses existing trusted success evidence where
available, and starts no Lean process in the default path.

### Authorized source-correction v2 and extension-audit subplan

This session preserves private Hub revision `88d768355b87a678be5fb37c5e677812f2614015`
byte-for-byte as superseded evidence. The already-authorized matched-500 ReForm run consumes a
different frozen 500-row input with zero affected library-docstring rows; monitor its durable state
without stopping, restarting, signaling, or changing it. No 10K/50K/full generation starts before
that run's runtime and quality report is available.

1. Replace the library `_adjacent_docstring` extractor with a deterministic nesting-aware Lean
   block-comment matcher. Fail closed unless the immediately preceding complete `/-- ... -/`
   doc-comment is structurally balanced after nested `/- ... -/` comments are consumed; keep a
   separate release canary that rejects any extracted NL containing literal `/-` or `-/`.
2. Freeze the exact v1 impact set and add regressions for all 92 corrupted rows: 54 Mathlib, 32
   Physlib, and 6 CSLib. Rebuild from pinned v1 inputs, demonstrate that no unaffected source or
   stable ID drifts without a recorded reason, and retain v1 paths/hashes/revision unchanged.
3. Detect obvious solution/proof discourse in Lean-Workbook with a versioned, explainable heuristic.
   Audit every heuristic hit and at least 100 deterministic rows from each of the seven release
   classes. Route confirmed discourse to a keyed auxiliary quarantine view; do not automatically
   discard all 293 flagged Workbook rows, and preserve per-row heuristic plus human-audit evidence.
4. Strengthen fresh-bundle verification to require the exact matched-view count, recompute the
   deterministic matched selection from full sources, validate every audit and quarantine record,
   and replay source/reference headless-signature, exact/near-duplicate, problem-identity, golden,
   ProofNet-family, ShadowBench, and benchmark-denylist evidence.
5. Emit a complete corrected core and separately keyed legacy tail under additive prefix
   `source_inputs/reform_diverse_full_v2/`. Validate locally from a fresh directory, upload only
   this prefix to private `Lemmy00/leanfaith-sft2-autoformalizer-v1`, then force-download the exact
   immutable Hub revision and replay all schemas, identities, selections, audits, and checksums.
6. Derive a full-source consumer from the matched-500 runner. Pin the new immutable Hub revision,
   source-view hash, prompt/tokenizer/model/placement/REPR identities, and four slots. Treat the
   corrected 50K core and remaining legacy tail as distinct resumable shards; require the complete
   source-by-four-slot Cartesian product, content-addressed terminals/caches, append-only journals,
   deterministic compaction, and explicit resource claims. Prepare and test dry-run/resume logic,
   but do not launch the detached tmux scale command until the matched-500 report clears the gate.
7. Produce a source-extension admission report without merging additions into the active v2 release.
   Prioritize pinned FrenzyMath `mathlib_informal_v4.19.0`/`Herald_proofs`, AgenticCommons
   `formal-math-autoformalization`, formal-mathfin, and exact-context theorem/docstring joins for
   SciLean, Stdlib, Batteries, PhysLean, CvxLean, Equational Theories, and axiom-clean solved
   FormalConjectures/FLT declarations. For each family, report net-new rows after current-release and
   benchmark deduplication, domain/style gains, trust tier, proof/`sorry`/axiom status, context
   recoverability, and a deterministic 100-row semantic-alignment audit. Exclude benchmark families
   and `by sorry`-only references.
8. Commit and push only the SFT2B brief and task-owned code/config/test/report artifacts. This
   authorization covers the corrected private source bundle only: no Lean corpus compilation,
   provider labeling, full generation, generated-output publication, or training.

Lean remains the bottleneck for downstream candidate validation. This correction does all parsing,
auditing, joins, deduplication, evidence replay, prompt counting, caching, and consumer dry runs
before Lean; it invokes no corpus compilation and never starts one Lean process per row.

### Authorized additive source-correction v3 and consumer-hardening subplan

This session keeps v2 and every matched-500 input/output byte immutable. Monitoring of the
independently frozen A100 run is read-only: never signal, stop, restart, alter, or duplicate it, and
use any eventual receipt only as pilot evidence. Neither corrected-core nor legacy-tail scale
authorization is requested or inferred here.

1. Freeze the exact 326-row v2 impact set (262 matched-core IDs and 64 tail IDs) and add fail-closed,
   versioned detection for explicit translation requests, retain-format instructions, direct-output
   commands, and related meta-instruction language. Preserve matched-v2 membership in the impact
   fixture and quarantine every confirmed hit outside the v3 source pool; no such row is relabeled
   as a semantic example.
2. Replace the v2 automatically generated “audit” terminology with an explicit deterministic-rule
   disposition record. Define a separate strict row-level review record containing reviewer
   identity/kind, method, timestamp, verdict, rationale, source hash, and reviewed fields. Require
   authentic completed review for all 293 Workbook heuristic hits and the deterministic
   100-per-release-class sample. Do not claim Codex-generated records are human review and do not
   substitute Opus/Terra unless the user explicitly changes the review contract.
3. Derive new deterministic quarantine rules only from confirmed review findings. Quarantine
   solution/proof fragments and incomplete or non-standalone library descriptions while retaining
   a keyed reason/evidence view. Fail closed if required review rows, hashes, reviewer fields, or
   rationales are absent or inconsistent.
4. Rebuild the full source pool, deterministic 50K matched core, and ordered tail from frozen v2
   inputs plus additive v3 rules. Emit a per-source v2-to-v3 conservation receipt that accounts for
   every retained row, removal, addition, dedup displacement, and core/tail movement and proves the
   partitions conserve the source universe. Publish only under a new additive v3 prefix after all
   review and verification gates pass; force-download the immutable revision and replay everything.
5. Replace the self-attested matched-pilot gate with an artifact verifier that opens and hashes the
   runtime, quality, output manifest, checksum ledger, journals, and terminal/shutdown/resource
   evidence. Bind exact frozen pilot inputs, model/prompt/tokenizer/placement/config identities,
   every 500-by-four request key, metrics, failure taxonomy, deterministic replay, clean shutdown,
   and released resource claim before the receipt can pass.
6. Integrate the actual resumable generation executor behind the task-owned full-source consumer.
   Reconcile provider attempts and terminal cells after crash/restart without duplicate calls,
   require durable journal and output advancement in detached health checks, and test complete
   Cartesian compaction. Keep core and tail authorization as independent fields with the invariant
   that tail remains false when core becomes true.
7. Add a pinned eight-A100 host profile rooted at `/scratch`, but keep both authorizations false and
   defer final run-ID computation until authorization state and the verified pilot receipt are
   frozen. Run no model server, provider, Lean, judge, training, or detached scale job in this work.
8. Commit and push only SFT2B-owned code/config/tests and this brief. Record exact local checks,
   unresolved human-review inputs, additive release evidence if publication becomes admissible, and
   the next non-scale gate.

Lean is the bottleneck. Complete all text filtering, schemas, source joins, review validation,
deduplication, conservation accounting, artifact hashing, journal reconciliation, and restart tests
without Lean. This subplan authorizes no corpus compilation and never starts one Lean process per
row.

### Authorized additive Opus/Terra source-review contract v4

The user explicitly replaces the pending-human-only gate with an additive model-panel option for
the frozen 992-row packet. The v3 human-review contract, packet, releases, and prior evidence remain
immutable. Model-panel records must be named and described as model review, never human or semantic
ground truth. This authorization covers contract implementation and one exact two-provider smoke;
it does not authorize reviewing the other 991 rows, building or publishing a corrected bundle, or
any generation, Lean, judging, training, or scale run.

1. Freeze a new versioned contract binding the existing packet bytes and source-use authorization,
   plus separate prompts for Claude Code Opus 5 (`opus`, high effort) and Codex GPT-5.6 Terra
   (`gpt-5.6-terra`, high effort). Record prompt/config/implementation hashes and live CLI identities
   before a call; provider aliases are floating and must be reported as such rather than invented as
   immutable server revisions.
2. Give each provider the same hash-bound source fields and rubric while blinding it to the other
   review, automatic inclusion/quarantine disposition, expected verdict, and downstream selection.
   Require strict structured provider output with verdict, standalone/alignment statuses, finding
   classes, confidence, and rationale. The wrapper—not the model—attaches reviewed-field/source
   hashes and provider lineage. Preserve raw request/response bytes in content-addressed storage.
3. Define the panel outcome deterministically: matching decisive verdicts admit that disposition;
   any disagreement, escalation, malformed output, provider failure, or insufficient confidence
   routes to an unresolved keyed view. Never use one provider to repair, summarize, or arbitrate the
   other, and never convert an unresolved row into keep or quarantine.
4. Implement append-only request/terminal journals, content-addressed caches, duplicate-call
   suppression, deterministic compaction, bounded timeouts, usage/cost provenance, and fail-closed
   reconciliation for ambiguous in-flight calls. External CLI stdin stays closed except for the
   exact prompt payload, and credentials never enter configs or logs.
5. Test schemas, prompt blindness, hash/version drift, malformed responses, disagreement,
   uncertainty, duplicate terminals, and crash/restart. Then select one deterministic packet row,
   run Opus and Terra independently, compact its panel result, and replay from cache with zero new
   calls. Report the exact record, outcome, hashes, latency, token/cost usage when available, and
   remaining risks before seeking authorization for the other 991 rows.
6. If a provider rejects the response schema before inference, preserve that terminal as immutable
   failure evidence and permit at most one additive transport-schema correction for that same row
   and provider. Prove from the event stream that no model answer was produced, retain the stricter
   parser-side validation, change the request identity, and reuse—not recall—any successful peer
   review. This exception does not permit retrying an ambiguous call or widening the row set.

Lean is the bottleneck downstream, but this contract change is entirely Lean-free: all schema,
prompt, provenance, hashing, cache, journal, and replay work is completed without compiling a corpus
or starting any Lean process.

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

- Supply the authentic 992-row human review, frozen reviewer/attestor identity allowlists, and the
  accountable external attestation, or explicitly change the contract before any Opus/Terra
  substitution. No model review is inferred from the missing human evidence.
- If they still exist on the independently managed eight-GPU host, supply the original
  shutdown/resource/zero-call-replay and fresh-publication receipt artifacts for the matched-500
  run. Their absence leaves that run useful as mechanically verified pilot evidence but does not
  authorize scale. No scale authorization is requested here.

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
- 2026-08-30 — pushed the complete task-owned setup to GitHub branch
  `milikic/sft2b-autoformalizer-setup`. Initial setup commit
  `32b8ea181a3ad81da7b289b6f3cf0d912f2eb427` contains only the claimed SFT2B brief, source,
  configuration, prompt, and test paths; unrelated EVAL2 worktree changes were excluded. A new
  machine should fetch this branch first, then consume the private Hub subset at the exact revision
  recorded above. The Hub `repro/workspace/` snapshot is a fallback transfer artifact, not a reason
  to overwrite a newer checked-out branch blindly.
- 2026-08-30 — resumed SFT2B on the authorized eight-A100 machine. Fetched and checked out
  `origin/milikic/sft2b-autoformalizer-setup` exactly at
  `b5aad18b4f818bb2a45731fdc675b3c3490d226e` with a clean worktree. Downloaded private Hub revision
  `878b3cab22883c732f05a5c30a9119d143e62489`, verified every `release/SHA256SUMS` entry, matched
  release-manifest SHA-256 `418d7cbad60dc1783ce1b68b91111876a7155d20f4fdae811068223d0011cef0`
  and the frozen release ID, then loaded all 10 configurations at counts 1/2/2/4/4/9/3/1/4/1.
  All four REPR pins and every derived live-file identity replayed before model work. The prior
  `/storage` mount is absent; per user direction all new durable data uses `/scratch/milikic/data`.
  The host exposes eight idle A100-SXM4-80GB GPUs with NV12 connectivity, 1.04 TB available RAM,
  and 1.75 TB free scratch. The existing machine-wide exclusive GPU reservation is now held by
  SFT2B at `/scratch/milikic/data/leanfaith/value_first/host_reservations/sft2b.json`; it covers all
  eight enumerated GPUs. No model download, load, generation, Lean call, or judging call had run at
  this boundary.
- 2026-08-30 — completed the authorized ReForm-32B GPU gate and bounded probe, then stopped all
  model processes and released the host reservation. The exact 65,540,277,627-byte BF16 snapshot
  at revision `80e9d9d83998d8c118c512bd6a35d1cdf11b57c8` replayed all 26 pinned file identities and
  snapshot binding `8dfcd12699d8a3730d1776dc6969e96ebe678d29ccaa43754e0deea3cd90773a`.
  vLLM 0.12.0 first served DP=1/TP=2 on GPUs 0--1 with max model length 4,312 and max one sequence.
  The one-source/slot-0 gate produced one strict-extractor candidate from 1,048 completion tokens in
  25,447 ms (40.96 output tokens/s, 120 ms TTFT), and its immediate replay returned the identical
  candidate and raw-response/output hashes with one cache hit and zero model calls. Gate root:
  `/scratch/milikic/data/leanfaith/value_first/sft2_autoformalizer_v1/generation/vllm/sft2b_reform_32b_smoke_dp1_tp2_v1/sft2b_vllm_run:98a241c8b0cc244a51faec3f736e2351aaf8e2e0cc3428158420a4c1f6681736`;
  initial-summary SHA-256 `1b6610d9f7623474aff716c45d5c7ea19d8151586e0f4519bbf54cef79813918`.
- 2026-08-30 — the all-GPU topology was DP=4/TP=2 on GPUs 0--7, max model length 4,317,
  `--max-num-seqs 16`, 0.9 GPU-memory utilization, and prefix caching disabled. Eight distinct
  source/slot requests (two sources times four frozen seeds) returned 6,198 completion tokens in
  32,503 ms: 190.69 aggregate output tokens/s, 0.246 requests/s, 147--213 ms TTFT, and
  8,661--32,433 ms per request. Every GPU reached 97--99% utilization; peak memory was 75,318 MiB
  on GPUs 0--1 and 74,680--74,683 MiB on GPUs 2--7. Queue depth stayed zero, minimum available host
  RAM was 1,009,946,214,400 bytes, and no request, server, or telemetry failure occurred. The frozen
  extractor admitted three candidates and isolated five outputs as `formalizer_output_contract`
  invalid; no invalid output became semantic `false`. Replay produced eight cache hits, zero model
  calls, and identical raw hashes in 108 ms. Probe root:
  `/scratch/milikic/data/leanfaith/value_first/sft2_autoformalizer_v1/generation/vllm/sft2b_reform_32b_probe_dp4_tp2_c8_v1/sft2b_vllm_run:12983be27261ab9d403711ae13270b05b8587cd499bf38eb56bafd2f1108adca`;
  initial-summary SHA-256 `845996c0f2829169e041c87c3f59bbeec19253a203cabbbfd7cdce5a40f08726`.
  At the measured 774.75-token mean, a four-slot 500-source ReForm run projects to about 2.26 GPU
  wall-clock hours (2,000 requests); this eight-request sample is too small for a cost commitment.
  No matched 500-source run, Lean compilation, judge call, 50K generation, publication, or training
  was launched. All GPUs were verified idle at 4--7 MiB before handoff.
- 2026-08-30 — user explicitly authorized completing the matched 500-source ReForm-32B generation
  before final publication/handoff. A local, private-Hub, and Git-remote search found no frozen
  500-source input on this machine: Hub revision
  `878b3cab22883c732f05a5c30a9119d143e62489` still contains exactly the two previously verified
  sources, and all eight of their frozen source/slot cells are terminal. The original-machine agent
  was therefore asked to publish an additive, checksum-bound 500-row `SourceRecord` manifest plus
  exact per-source prompt-token counts. No model server was restarted and no decoding seed/source
  contract was invented while that required input is absent. The vLLM implementation and bounded
  evidence are being pushed now so the source-freeze agent can target the live interface.
- 2026-08-30 — user authorized preparation and additive private publication of the frozen matched
  500-source pilot input because the transferred two-source smoke release blocked the 8xA100
  generation session. Claimed the existing SFT2B paths only, set status `active`, and recorded the
  executable source-freeze subplan above before catalog work. This session will run no ReForm, Lean,
  judges, output publication, or training; all unrelated EVAL2 work remains untouched.
- 2026-08-30 — completed the authorized, Lean/GPU-free matched-500 source freeze. The deterministic
  audit examined 577 Mathlib docstring records, all 33,027 pinned Numina train rows, and all 25,214
  pinned Lean-Workbook rows; it replayed source-use-v2, the exact 301-candidate/50-reference
  DATA-REUSE receipt and consumed-bundle hash, source file/card/license hashes, strict contexts,
  stable IDs, global deduplication, and canonical golden exact/near/problem screens. ConsistencyCheck
  remained evaluation-only, ShadowBench remained reference-free/test-only, and selected overlap
  counts were all zero. The exact mix is 175 library docstrings, 175 theorem problems, 100 broader
  public/synthetic rows, and 50 specialist/high-difficulty rows. Exact ReForm-32B tokenization found
  967 maximum prompt tokens, so the smallest safe `max_model_len` is 5,063 with 4,096 completion
  tokens. Task-owned verification passed 24 unit tests, Ruff, format check, and strict mypy; no
  ReForm, Lean, judge, generated-output publication, or training action ran.
- 2026-08-30 — pushed the source-freeze builder on branch
  `milikic/sft2b-autoformalizer-setup` at commit
  `bb49f980dbe5234efc34d4aefc2b007730b6c642`, preserving the concurrently added eight-GPU vLLM
  backend. Published exactly four additive input files under
  `pilot_inputs/reform_matched_500_v1/` in private dataset
  `Lemmy00/leanfaith-sft2-autoformalizer-v1` at immutable revision
  `08aa352a1e6c80f7c98f63070f0351ad39f8a272`; the prior revision
  `878b3cab22883c732f05a5c30a9119d143e62489` remains unchanged. A pre-upload clean-directory
  replay and a second fresh download from the immutable Hub revision both revalidated all 500
  SourceRecords, every prompt hash/token count, the source manifest, and all checksums. Status is
  `pilot_ready` for the already-authorized 2,000-request DP=4/TP=2 ReForm-32B generation.
- 2026-08-30 — added the fail-closed matched-500 one-command runner at
  `leanfaith.sft2b.matched_500_pipeline` with frozen config
  `configs/sft2b/reform_32b_matched_500_pipeline_v1.json`. The A100 invocation is
  `uv run --with vllm==0.12.0 python -m leanfaith.sft2b.matched_500_pipeline`. Before server
  startup it verifies the private immutable four-file input, all 500 strict sources and token rows,
  source mix/contamination/ShadowBench exclusion, task code, REPR, prompt, placement, tokenizer,
  every model file, 5,063-token context, exact slots/seeds, vLLM version, and eight 80-GB GPUs. It
  starts one vLLM server at DP=4/TP=2 with concurrency 64, reuses complete content-addressed cells,
  writes the append-only journal and telemetry, requires the exact 500x4 Cartesian product, and
  publishes raw generations/attempts/candidates/formalizer-invalid views additively before a fresh
  immutable-revision download check. It runs no Lean, judges, labels, core rows, publication to a
  public repository, or training. Offline unit checks passed and the command's own live Hub input
  downloader reverified revision `08aa352a1e6c80f7c98f63070f0351ad39f8a272` at 500 rows and 967
  maximum prompt tokens; no model download, GPU server, or generation ran on the original machine.
- 2026-08-31 — claimed the SFT2B full-source freeze on branch
  `milikic/sft2b-full-source-freeze` and expanded the requested 50K cap to every row surviving the
  same source-quality contract. The Lean/GPU-free audit now joins a frozen 176,101-row closed-type
  census to strict adjacent human docstrings across Mathlib, Physlib, and CSLib; audits all 104,155
  rows of public `AI-MO/NuminaMath-LEAN`; replays all 33,027 owner Numina and 25,214 Lean-Workbook
  rows; and replays the 301-candidate exclusion receipt. The raw qualified pools contain 14,298
  library rows, 33,470 current Numina rows, 27,464 legacy owner Numina rows, and 8,501 Workbook
  rows. Priority-ordered global proposition/NL/signature deduplication plus existing-301 and golden
  screens leave 54,455 unique sources. ProofNet, ProofNetVerif, ProofNetSharp/ProofNet#, every
  derived or mixed ProofNet variant, and ShadowBench are blanket-excluded; the ProofNet-containing
  `iiis-lean/lean-math-formal-corpus` aggregate is audit-only and contributes no row.
- 2026-08-31 — the required serialized 1-row, 100-row, and 10,000-row gates passed after one 10K
  gate exposed and fixed a substring-based prompt leak false-positive before writing output. The
  100-row view represented all seven release classes. The successful 10K run took 108.92 seconds,
  peaked at 1,944,936 KiB RSS, produced exact prompt hashes/counts, replayed all strict
  `SourceRecord` identities and bundle checksums, and observed 739 maximum prompt tokens
  (`max_model_len` 4,835 with 4,096 completion tokens). No Lean, ReForm, judge, label, generated
  output publication, or training process ran.
- 2026-08-31 — committed and pushed the source-freeze implementation/config/tests first as Git
  commit `556284b4967c857e754a727ffe1bb8f02eadc453` on
  `milikic/sft2b-full-source-freeze`, then built the manifest against that retrievable revision.
  The full build completed in 133.52 seconds at 2,308,148 KiB maximum RSS and serialized all 54,455
  surviving sources plus a deterministic 50,000-ID matched view. The release source mix is 12,514
  Mathlib, 514 Physlib, 336 CSLib, 8,328 Lean-Workbook, 10,136 current human/mixed Numina, 1,183
  current auto-proof Numina, and 21,444 additional owner Numina rows. Maximum prompt length is 967
  tokens, so the pinned 4,096-token completion budget requires `max_model_len = 5,063`.
- 2026-08-31 — published the six-file source-only bundle additively to private dataset
  `Lemmy00/leanfaith-sft2-autoformalizer-v1` at immutable revision
  `88d768355b87a678be5fb37c5e677812f2614015` under
  `source_inputs/reform_diverse_full_v1/`. A forced fresh download from that exact revision replayed
  file checksums, strict schemas, all stable source IDs, prompt hashes and token counts, source mix,
  REPR/runtime pins, the 301-candidate exclusion receipt, and zero selected golden, ProofNet-family,
  or ShadowBench hits. `sources.jsonl` is
  `c012fe688f01e36a0a7a76ffe2e4a0b2f0090ed3d7a0919c27fab6c93b673564`;
  `prompt_token_counts.json` is
  `d7e97e5a7729733f64e74cff4d5bf4f65b1d82bd73e2a00c6bf8db8b03d65916`;
  `source_audit.jsonl` is
  `8ab01a590767dc7a9e76b9ece2054f058c0eeb619d2ab895e5d71edf9d592722`;
  `matched_50000_source_ids.json` is
  `7fd9e3386207ed725f68e34e66036df57bf9f943b4c6decec3bb3590179b22b2`;
  `source_manifest.json` is
  `4f2004b3db4b0d03283e155489dc93f92a005ec5a891f1c627165a7a85813a88`; and `SHA256SUMS` is
  `2eb3d08ec1c37fee54847541ebdd07eb377fdc8853dcda1910cc295b0f293c5d`.
- 2026-08-31 — claimed the additive source-correction v2 and source-extension audit on branch
  `milikic/sft2b-source-correction-v2` before implementation. Revision
  `88d768355b87a678be5fb37c5e677812f2614015` remains immutable superseded evidence. The active
  matched-500 input is independently frozen and contains zero of the 92 discovered docstring
  corruption rows, so this session only monitors its journals/process health and will not interrupt
  it. The executable subplan above gates parser correction, exact regressions, selective Workbook
  quarantine, stronger fresh verification, additive v2 publication, a prepared-but-unlaunched
  two-shard consumer, and a separate extension admission report. No Lean, provider, scale-generation,
  generated-output publication, or training action is authorized.
- 2026-08-31 — replaced the v1 library docstring lookup with a nesting-aware Lean block-comment
  scanner that records only balanced top-level documentation comments outside attributes, strings,
  and line comments while retaining an independent fail-closed `/-`/`-/` model-facing canary. An
  exact frozen impact fixture replays all 92 corrupt v1 rows (54 Mathlib, 32 Physlib, 6 CSLib); all
  92 now fail closed. Regression coverage also exercises ordinary intervening comments, nested
  comments, attribute-internal docstrings, and physical line locators across multiline comments.
  No Lean process was started.
- 2026-08-31 — completed the full corrected source-only probe and its strengthened verifier. The
  corrected pre-quarantine pool contains 54,906 rows; nesting-aware recovery increases legitimate
  Mathlib rows while removing every corrupt delimiter-bearing row. The frozen Workbook heuristic
  still finds exactly 293 selected rows: an audit quarantines 285 solution/answer-discourse rows in
  an auxiliary full-row view and retains eight explicit prove-that claims/questions. The final core
  has 54,621 rows (13,003 Mathlib, 482 Physlib, 330 CSLib, 8,043 Workbook, 10,136 current-human
  Numina, 1,183 current-auto Numina, and 21,444 legacy-owner Numina), with an exact deterministic
  50,000-row matched view and a disjoint 4,621-row tail. The semantic sidecar contains 992 unique
  audit rows: exactly 100 deterministic rows for each of seven release classes plus every one of
  the 293 Workbook hits. Maximum prompt length remains 967 tokens and the required model length is
  5,063. Fresh verification replays every strict sidecar row, original headless signatures, closed
  propositions, near hashes, problem identities, golden/existing-301 screens, prompt tokens, exact
  matched selection, tail partition, schemas, and checksums.
- 2026-08-31 — prepared but did not launch the gated two-shard full-source consumer. It freezes the
  four slot/seed Cartesian product, content-addressed run/cell/cache identities, locked append-only
  journals, duplicate suppression, deterministic complete-only compaction, resource-supervised
  detached tmux construction, and durable startup-health evidence. Its checked-in status remains
  `waiting_matched_500_report`; v2 input pins are filled only after additive publication, and actual
  launch remains impossible until an exact passing matched-500 runtime/quality receipt plus later
  scale authorization are recorded. The A100 host is not observable from this machine; Hub has no
  output receipt yet, so the run was neither interrupted nor duplicated and the scale gate remains
  closed.
- 2026-08-31 — published the corrected source-only bundle additively under
  `source_inputs/reform_diverse_full_v2/` in private dataset
  `Lemmy00/leanfaith-sft2-autoformalizer-v1` at immutable revision
  `d0b961d2112d186009984242db674f2ad59905c7`. All 11 files were rebuilt against pushed Git commit
  `637ee92`, verified from a separate clean directory before upload, force-downloaded from the exact
  Hub revision, and fully replayed afterward. `sources.jsonl` is
  `1e53fd731822d3c69e1395f934f1849257a3565517a09c38a50f7e3589a851c7`, the exact 50K view is
  `49a0cee8a90e048eb7f9b1c18b7e6cb85e0bff3dbe7dfa70d8dde1865d3ae4ab`, the ordered 4,621-row tail
  is `7eecf134f734ea9275a30b03fb7c230824406a085d1394dcfbd17df2de4aba64`, and `SHA256SUMS` is
  `34554e7c6f39427ce230153ba8f04e83f5b1fd0b500e1847bd8dbbd502d8c608`. Superseded revision
  `88d768355b87a678be5fb37c5e677812f2614015` still resolves with its original six v1 files and
  original `SHA256SUMS` hash `2eb3d08ec1c37fee54847541ebdd07eb377fdc8853dcda1910cc295b0f293c5d`.
- 2026-08-31 — pinned the full-source consumer to the immutable corrected Hub revision and replayed
  both downloaded ID views without launching anything. Core preflight expands exactly 50,000
  sources to 200,000 four-slot cells with run ID
  `sft2b_full_reform_run:35f665ea7bfa8b61a8c61726cb97bd7d6899acfc9e524895b6c74d87b0c159fb`;
  tail preflight expands 4,621 sources to 18,484 cells with run ID
  `sft2b_full_reform_run:9ff78e7ab94ef0769397872c0991f6e670c1c822f40c39313ebc3cbc91f51533`.
  Both receipts report `launch_authorized=false` and `waiting_matched_500_report`; no tmux session,
  resource claim, GPU process, or generation call was created.
- 2026-08-31 — completed the separate, non-admitting extension report at
  `configs/sft2b/source_extension_admission_v1.json`. Lean-free current-release/benchmark screens
  find 141,733 priority-incremental generated-dataset candidates (135,453 Mathlib-informal, 3,894
  Herald after Mathlib dedup, 2,055 AgenticCommons, and 331 formal-mathfin) plus 1,260 conditional
  library candidates (455 PhysLean, 331 Stdlib, 314 FormalConjectures, 53 Batteries, 47 FLT, 45
  Equational Theories, 13 CvxLean, and 2 SciLean). Every family has a deterministic 100-row audit,
  or every surviving row when fewer than 100 exist. None is admitted: generated-NL faithfulness,
  explicit benchmark normalization, exact source/context recovery, multi-claim projection, and
  bounded kernel axiom checks remain family-specific gates; ProofNet/ShadowBench and by-sorry-only
  references stay excluded.
- 2026-08-31 — claimed the additive source-correction v3 and full-consumer hardening on branch
  `milikic/sft2b-source-correction-v2`, preserving all v1/v2 and independently frozen matched-500
  artifacts byte-for-byte. Status is `active`. The executable v3 subplan above was recorded before
  implementation: first freeze the exact 326 meta-instruction impact rows, then separate automatic
  rule dispositions from authentic row-level review, require review before v3 publication, emit a
  complete v2-to-v3 conservation receipt, verify real pilot artifacts rather than self-attestation,
  integrate and crash-test the resumable executor, require actual durable advancement, and keep core
  and tail authorization independently false. Lean is the bottleneck; no Lean, model, provider,
  judge, detached scale job, generated-output publication, training, or scale-authorization request
  is permitted in this session.
- 2026-08-31 — correction to the preceding kickoff record: the additive v3 work was moved to and
  completed on pushed branch `milikic/sft2b-source-correction-v3`; kickoff commit `15a9594` was
  recorded before substantial execution. The exact originally reported meta-instruction impact is
  frozen at 326 rows (262 matched core and 64 tail), fixture SHA-256
  `8dc3e66023d687405bb77e4e811a2eea4dc79b4846e534db0d1afbbfd2604c25`.
  The fail-closed active rule set finds 469 rows (394 core and 75 tail), including 143 additive
  strict hits, at fixture SHA-256
  `44566540c96adc0ab96ca6aa4a8e8ae757edcc75a863fe5524fbd48689ee50ab`.
  Automatic rule dispositions remain explicitly mechanical and non-semantic.
- 2026-08-31 — generated the authentic-human-review packet without performing or simulating its
  review. It contains 992 unique rows: all 293 Workbook hits plus deterministic 100-row samples
  from each of seven release classes, with one row overlapping those requirements. Packet SHA-256
  is `77bc24c8372c55e1698f60a5d3fd715d56fd1fb277ede7acfc577cc812c9fd6d` at
  `/storage/milikic/leanfaith/value_first/sft2_autoformalizer_v1/source_reviews/source_review_contract_v3_pending_human`.
  No human verdict, reviewer/attestor allowlist, external attestation, Opus call, Terra call, or
  other model substitution exists. The additive v3 builder, config, and tests are pushed in commit
  `4c1a65a`; builder SHA-256 is
  `233b8980b12f4be8f220dff2a0c700cd25281b034c1a3924404c7553acabb8c1` and config SHA-256 is
  `0d2777163cdb74f678ab062892e210925378cc1cfa6cfe8ffbffefa3b8f86b12`.
  An unmocked preflight replayed the real 54,621 active plus 285 quarantined v2 rows and stopped
  only at the missing authentic-review/attestation pins, with return code 1 and no output path.
  Hub revisions `88d768355b87a678be5fb37c5e677812f2614015` and
  `d0b961d2112d186009984242db674f2ad59905c7` remain byte-immutable; no v3 bundle was built or
  published.
- 2026-08-31 — consumed the completed matched-500 generation read-only without restarting it.
  Private Hub revision `e7f2ef6e5c84f22de479cf99360aace281523a71` contains exactly 11 files
  under `pilot_outputs/reform_32b_matched_500_v1/d9eadc4aa717813e61d9809a98ba771e7004c1fc9161caa97a7c020b4746d387`;
  the remote revision still resolves exactly and a local immutable-revision download passes every
  published checksum. The hardened verifier streamed and replayed the 1,331,118,638-byte raw SSE
  artifact, all 500-by-four ordered request keys, request/terminal/attempt joins, extraction,
  routing, model/config/git/input/REPR pins, and observed eight-H100 telemetry. It found 1,242
  strict output-contract admissions and 758 output-contract rejections, not Lean-validity or
  semantic labels; per-slot admissions are 322/314/300/306, 1,147 signatures are globally unique,
  and 246 requests ended at the length limit. The frozen selection mix remains 175 library
  docstrings, 175 theorem problems, 100 broader public/synthetic, and 50 specialist sources.
  Partial-evidence binding is
  `20e275a9bd842ba158bd71be21b6680de44d9bd7cfe424f98bd90a83c349b7ad`.
- 2026-08-31 — the observed matched-500 receipt is deliberately `gate_passed=false` and
  `quality_decision=not_authorized`. The 11-file publication does not contain independently
  replayable clean-shutdown, process-absence, resource-claim, resource-release, true zero-call
  cache-replay, explicit quality-acceptance, or fresh-download publication-receipt artifacts. The
  user's pipeline JSON reports 2,000 fresh model calls and `fresh_verification=true`; those useful
  facts are preserved as externally reported evidence but are not converted into missing receipt
  files or a scale pass. The actual manifest reports H100s, so it is not relabeled as A100 runtime
  evidence.
- 2026-08-31 — hardened the unlaunched full-source consumer around the real integrated vLLM
  executor. It now streams compaction and pilot SSE replay; journals provider starts and terminals;
  refuses ambiguous in-flight calls; records append-only runtime start/close and multi-session
  resource claim/release evidence; verifies every historical pair before completion; requires
  nonce/PID plus actual journal/output advancement for detached health; and routes complete caches
  without sufficient runtime evidence to `recovered_unattested`. The host-specific eight-A100
  profile consistently uses `/scratch`, matching the already-frozen runner on the host where
  `/storage` was absent. Frozen authorization requires complete pilot artifacts and exact evidence
  binding; the checked config remains `active` and unfrozen with both core and tail disabled, both
  final run IDs deferred, and `launch_authorized=false`. Consumer SHA-256 is
  `7acab628001f43a23473256529d349d0f127bee07219e655e3fda46e7cd547e9`; config SHA-256 is
  `24aac54b6c70e7e6883e18b6d9f5d51d2f8951226c32142a61590235dfe7b205`.
  The focused consumer suite passes 23 tests and the complete SFT2B suite passes 121; Ruff lint and
  format checks, strict mypy, diff checks, both disabled-shard preflights, the authentic-review
  fail-closed preflight, and the real 1.331-GB pilot replay all pass. No full or tail generation,
  model server, provider call, Lean process, judge, tmux scale job, generated-output publication,
  training action, or scale-authorization request ran in this session.
- 2026-08-31 — all authorized local v3 and consumer-hardening work is exhausted, so the task moves
  from `active` to `waiting_user` only at this boundary. The next input is authentic review evidence
  (or an explicit review-contract change) and, independently, any genuine missing pilot closure
  receipts that still exist on the generation host. This is not a request for scale authorization.
- 2026-09-01 — the user explicitly authorized the alternative Opus/Terra review contract. Status
  returns to `active` under the additive v4 subplan above; the frozen v3 pending-human contract and
  every v1/v2/pilot artifact remain immutable. The bounded implementation gate is one deterministic
  packet row reviewed independently by Claude Code Opus 5 and Codex GPT-5.6 Terra at high effort,
  followed by strict consensus/unknown compaction and a zero-provider-call restart. This is model
  panel review, not human or semantic ground truth, and does not authorize the other 991 reviews,
  corrected-bundle publication, generation, Lean, judging, training, or scale authorization.
- 2026-09-01 — implemented the additive model-panel contract without changing v3. Contract SHA-256
  is `579acee1dcaf3d2f9f49fb688018600c814637b67b7b0e2f75c35420ae3764f5`; implementation
  SHA-256 is `1c05607138896c385867795e99add457bd541b1a35d03f7bc73fcdfd92e00101`;
  response-schema SHA-256 is
  `c7fa9a66caf52cd0b97562aa14f078e8f3317ee6e410391eecfe429418401e31`; Opus/Terra prompt
  SHA-256 values are respectively
  `8c537319c017ebe87ebe9062e2962f27920e633d18f4bcfcc54cf85eca648c89` and
  `cb8d40b72903117a5b8360874f27a03c58d3379cde0fedf67202db129d1a1255`.
  The real no-call preflight replayed all 992 packet rows and every packet/policy/prompt/schema/
  implementation/binary/CLI pin. The authorized row is packet entry
  `sft2b_review_packet:f1fbb98fa666e7e46187d31463fb649d1f7565c1db500cb2c51f62cd8b82631c`,
  a public Apache-2.0 Lean-Workbook solution-discourse example; its selection reason and automatic
  disposition are omitted from both model projections. Focused tests pass 9/9, Ruff passes, and
  strict mypy passes. No provider, Lean, generation, publication, judging, or training call has run
  at this boundary.
- 2026-09-01 — ran the exact one-row v4 smoke from pushed commit `d400e1e`. Opus returned a valid
  `quarantine_solution_or_proof_fragment` review at confidence 0.86 in 19.12 seconds, reporting
  4 input, 3,173 cache-read, 4,390 cache-creation, and 1,593 output tokens at $0.0879335. Terra's
  request terminated in 2.89 seconds with HTTP 400 `invalid_json_schema`: the provider rejected
  `uniqueItems` before any model item, answer, or usage event. The fail-closed panel outcome is
  `unknown_provider_failure`; its run ID is
  `sft2b_model_review_run:cfb529a7d7a86e6e0d45d797f7d685a8ef665dfc1caea75b8d09e3bbf3e3e604`.
  A restart produced two cache hits, zero provider calls, the identical manifest hash
  `480fceee78f3399a5cb1aea07069215e911006f266c8285cd27d4890554de546`, and no ambiguous
  request. The additive retry rule above now permits one Terra-only transport correction on this
  row; the successful Opus record must be reused and the initial run remains immutable.
- 2026-09-01 — froze the one-call Terra correction under retry-config SHA-256
  `b9a42a9a8adc7bca5c322ceef3efd166dcaf9c97c624329c924a1e4a8dc94a49`, retry implementation
  SHA-256 `42dee2fec55d6de9d4ed1b7147e819463214b2276cee2e45e8c49620d1a5122d`, and transport-schema
  SHA-256 `78102363f993015abbfc3cd84d8da54392410a5f215003223d8825af3fce3f8a`.
  Its no-call preflight reverified the original config/run/output/checksum/cache hashes, proved the
  exact `thread.started`, `turn.started`, `error`, `turn.failed` pre-inference event shape, proved
  there was no item or usage event, and mechanically showed that the transport schema differs only
  by removing `uniqueItems`; the Pydantic parser still requires unique sorted issue classes. The
  runner exposes only one Terra cell and explicitly records zero Opus recalls. Twelve focused v4
  and retry tests, Ruff, and strict mypy pass. No retry provider call has run at this boundary.
- 2026-09-01 — the single corrected Terra request succeeded in 8.71 seconds with 12,345 input,
  290 output, and 196 reasoning-output tokens; the provider did not report dollar cost or a resolved
  immutable model revision. It independently returned
  `quarantine_solution_or_proof_fragment` at confidence 0.96. Combined with the frozen Opus review,
  the deterministic panel result is unanimous `consensus_quarantine`, final disposition
  `quarantine_solution_or_proof_fragment`, outcome ID
  `sft2b_model_panel_outcome:008acf60c7f73009a1089b74b0abd43bf7d50a945bd7d8b5c7f4f587396cb746`.
  Retry run ID is
  `sft2b_terra_retry1_run:b16e5bfab444f38214f4ac41afcb1813864fa5e812bff875a0c5650b0b9ff375`;
  manifest SHA-256 is `c0d8ea456897bd588972c37565ae24b61cd1fc651549808b788481b97446e5a1`,
  checksum-ledger SHA-256 is
  `93a51a6d66e65198520e6f151262c8bfe930daee2fc01ee17bb2782be1e32a7d`, and journal SHA-256 is
  `f022396655d217bdd8e7bf10771bfa4bf31f6da004839f0273b0573f98e0bc19`. The retry used one
  Terra call and zero Opus calls; its restart used one cache hit, zero provider calls, reproduced
  the identical manifest, and left zero ambiguous requests. Full artifact verification passes.
  The complete SFT2B unit suite passes 133/133; focused Ruff/format checks and strict mypy pass.
  No other packet row, Lean process, generation, judge, publication, training, or scale job ran.
  The bounded local contract work is exhausted, so status moves to `waiting_user`; this is not a
  request for generation-scale authorization.

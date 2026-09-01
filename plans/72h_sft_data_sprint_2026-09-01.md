# 72-hour SFT data sprint — 2026-09-01

This is the active cross-task execution contract for SFT1, SFT2A, and SFT2B. It incorporates the
code-grounded parts of the GPT Pro and Claude Fable 5 reviews at integrated commit
`0efbaf522dc72b3f56ffd04a812dea623ae98c00`.

The integration checkout is `/localhome/milikic/LeanFaith-main-integration` on local branch `main`.
The original `/localhome/milikic/LeanFaith` checkout contains unrelated in-progress EVAL work and
must not be switched, cleaned, or used for these SFT edits.

The goal is data, not another review cycle. Preserve historical configs, receipts, plans, and
outputs as immutable evidence, but remove them from the launch dependency graph when they do not
protect label correctness, resumability, or an expensive run.

## Sprint authority and stop policy

Passing the objective conditions below authorizes the next named stage automatically. Agents do
not stop for a new exact authorization sentence, clean-worktree receipt, full-tree hash, repeated
pilot replay, or another model review. They update their task brief, commit the implementation,
launch any long authorized stage in a verified named `tmux` session, and leave it running.

Stop only for one of these reasons:

1. evidence indicates a potentially wrong core label;
2. a run longer than one hour cannot safely resume without duplicate expensive calls;
3. the measured yield or throughput misses the explicit threshold below;
4. the shared two-worker/40-GiB Lean or external accelerator allocation is unavailable; or
5. an input, model, prompt, or output identity needed to reproduce data is unknown.

Provider spend is measured and guarded against accidents, but is not a reason to choose weaker
models. New judging uses Opus 5/high, GPT-5.6 Terra/high, and Kimi 2.7/high. No SFT1 label uses an
LLM.

## Required outputs by T+72h

- **SFT1:** target a complete, privately published Mathlib sprint wave over the seven operations
  below. The guaranteed minimum is a sound 10K retained-pair release plus independently complete
  larger shards and a healthy resumable full-wave job with a measured ETA. Do not claim that the
  older 2–3M multi-source target can finish in 72 hours until the 10K throughput measurement says
  so.
- **SFT2A:** complete and compact the 10K-root run as ten independent 1K-root shards, or leave only
  its already-healthy final shards running with exact progress and ETA. Do not rerun the completed
  100-root generation.
- **SFT2B:** finish and privately publish all four corrected-core ReForm generation shards:
  50,000 sources × four slots = 200,000 request terminals. Validate the downstream Lean plus
  three-judge consumer and start it as generation shards arrive. Full semantic labeling may exceed
  72 hours because up to 600,000 judge calls are possible; report measured throughput rather than
  pretending otherwise.

## SFT1 — compact proof-backed deterministic path

### Scope

Freeze the historical `Wave1.lean` and its policy/readiness/admission/identity/census stack. Do not
repair or delete those hash-bound artifacts. Build an additive compact engine from the working
thin-smoke route with single-hop Mathlib transforms only:

1. `P15_SWAP_IFF_SIDES_V1`
2. `P18_SYMMETRIZE_EQUALITY_V1`
3. `P14_SWAP_INDEPENDENT_DATA_BINDERS_V1`
4. `P23_CURRY_PROP_PAIR_V1`
5. `N25_TOGGLE_EQ_NE_PROOF_V1`
6. `N32_SWAP_ROLE_ORDER_PROOF_V1`
7. `N31_DROP_REQUIRED_GUARD_PROOF_V1`

If P14 takes more than two implementation hours or cannot retain ten pilot pairs, substitute P24
with the same dependency/independence checks. P01, P21, composition, rubric-only N31, and the
four-project conformance matrix are outside this sprint. P32/P35 are the first later expansion only
if the 10K shortcut screen shows that the positive set is too superficial.

Every positive row carries a replayed, type-checked `Iff reference candidate` witness. Every
negative row carries a checked source proof and checked `Not candidate` refutation for the exact
closed pair. N25 requires a complete grounded assignment. N32 initially supports strict Nat/Int
`<`; `≤` drops unless strictness/disequality evidence supplies a refutation. N31 is limited to
bounded Nat/Int guards with a checked boundary counterexample. No refutation means no core row.

The runner preselects roots without Lean, reads a semantic cache before Lean, batches work through
one persistent Mathlib worker, writes append-only root/operation terminals, replays every proof and
certificate, screens gold/self/duplicates, and compacts deterministically. Its cache key binds the
root closed-Expr hash, operation ID, engine semantic version, Lean/project revision, and
imports/options context; runner/config source bytes are provenance, not cache identity. Reject
`[anonymous]`, `⋯`, and `✝` on ordinary explicit locals. Frozen generated instance names such as
`inst✝` remain allowed and are counted separately.

### Automatic gates

Compile the compact engine, then run one success and one typed rejection fixture per operation.
On success, run 100 deterministic Mathlib roots and inspect 30 stratified retained pairs, including
every N31 row. There is no separate 10–20-root gate.

The 100-root run passes only with:

- at least five mechanisms retaining at least ten pairs each, including at least three positive
  and two negative mechanisms;
- checked equivalence for every positive and checked source proof plus candidate refutation for
  every negative;
- 100% certificate/proof replay and zero rubric-only negatives;
- zero `[anonymous]`, `⋯`, or ordinary-local `✝` in core rows;
- zero extra Lean calls or duplicate rows on terminal replay;
- zero wrong labels in the 30-row manual inspection; and
- roughly one hour or less on one persistent worker, otherwise optimize batching first.

A pass automatically starts a detached, resumable 10,000-retained-pair run in 1,000-pair shards.
The 10K release requires 100% replay, no duplicate/conflicting unordered pair, grouped ancestry,
at least two useful negative mechanisms, candidate-only and reference-only balanced accuracy below
0.60, and family/mechanism-held-out balanced accuracy below 0.65. A pass automatically starts the
full Mathlib wave in independently publishable shards when projected completion fits the remaining
sprint window.

## SFT2A — decouple provider throughput from Lean

Keep independent per-slot Terra calls for this sprint. They preserve slot-local retries, Lean
feedback, caches, and accepted siblings. Concurrency solves the measured latency without adding a
four-slot envelope. Reconsider batching only if 1K-shard throughput is below eight accepted rows
per minute at concurrency 16 without sustained throttling.

Required fixes:

1. Persist parseable provider output as a terminal even when schema-invalid; retry malformed output
   once, then route it to unknown/slot retry rather than crashing the run.
2. Keep semantic validation strict. Define `error_type`/`unknown_reason` explicitly; a binary
   verdict with uncertainty metadata is unknown, not silently accepted.
3. Resume the existing 40-row Kimi audit at concurrency eight with one durable row per judgment;
   do not regenerate the 284 completed core rows.
4. Declare canonical `u_0`…`u_7` universes, turn remaining level metavariables into parameters,
   bump the oracle method version, bind all displayed section variables in the proposer prompt,
   and remove source-file bytes from the semantic cache key.
5. Use a dynamic queue: eight roots in flight for the pilot, then sixteen after a clean pilot;
   exactly two locked persistent project-keyed `SignatureOracle`s, reused with `rebind`.
6. Claim global deduplication only after Opus accepts a row. Mechanism mismatch is telemetry, and
   `definitional_unfold_refold` leaves the rotation.
7. Before 10K, load ledger/root/dedup state once per process and append under short locks, or use
   SQLite. Interrupted in-flight calls are retryable under the same semantic key.

After focused tests, run 20 unused certified roots/80 slots. Pass conditions are: 20–30 minutes or
less, Lean-invalid below 25%, accepted slots at least 70%, no accepted self-pairs/duplicates, a
malformed injected answer does not abort, and completed roots resume with zero provider/Lean calls.

A pass automatically certifies roughly 12K references and launches ten independent 1K-root shards.
Shard 1 starts at provider concurrency 16 and falls to eight on sustained throttling. Shards 2–10
continue automatically if provider failures remain below 2%, accepted throughput is at least eight
rows/minute, and invalid/acceptance rates remain inside pilot bounds. Kimi sampling, mechanism
agreement, 2% cache replay, and manual row inspection are asynchronous telemetry; observed
disagreements are excluded from the audited view but do not serialize generation.

## SFT2B — corrected full-core ReForm generation

### Mechanical source v3

Add an explicit `mechanical_conservative_v1` mode; never invent human review records. From the
current v2 universe of 54,906 rows, quarantine the union of 469 meta-instruction hits and all 293
Workbook hits. Of those Workbook rows, 285 are already quarantined and eight are active; overlap
with the meta set is zero. The expected result is:

- 762 total quarantine rows;
- 54,144 active rows;
- 49,598 surviving prior-core IDs plus 402 prior-tail IDs, in that boundary-preserving order;
- exactly 50,000 core and 4,144 tail rows.

Emit and verify conservation plus core/tail/quarantine release-class and domain mixes. The expected
new core release-class counts are Mathlib 13,003; Physlib 482; CSLib 330; Workbook 8,035;
current-auto Numina 1,154; current-human Numina 10,046; and legacy-owner Numina 16,950. Fresh-verify
the additive private Hub release.

### Full matched-pilot audit and resumable consumer

Compile all 1,242 admitted matched-500 candidates, not a 200-row sample. Group the 500 references
by their 35 render/36 source contexts and reuse persistent tolerant Lean sessions. The audit passes
only if every trusted reference elaborates, at least 500 candidates elaborate (at least 40% of
admitted and 25% of all requests), at least 250 sources have one valid candidate, infrastructure
failures are below 2%, and class/context/extractor failure histograms are recorded.

Before scale, change generation so semantic request identity contains only source ID, slot, seed,
model snapshot, prompt/template input, and decoding parameters. Config/Git/authorization hashes are
provenance. Retry transient requests up to three times; reconcile started-without-terminal only
after the exact old server is absent/terminated; recover stale same-run reservations/ports by PID
and run identity; accept interrupted runtime sessions; and keep one sliding request window across
input chunks.

Prove this with a 100-source/400-cell accelerator smoke that kills both vLLM and its supervisor,
then resumes without manual cleanup, duplicate logical requests/terminals, or missing cells. A pass
automatically launches four contiguous 12,500-source shards sequentially on eight GPUs in named
`tmux` sessions. Each shard has 50,000 requests, requires at least two requests/second, compacts and
publishes independently, and feeds downstream consumption immediately.

In parallel, reuse all 1,242 Lean results and send 100 deterministic valid unique candidates to all
three blinded default judges with concurrent per-vote terminals/caches. The downstream pilot passes
with unknown at most 25%, pairwise agreement at least 70%, correct majority/unknown routing, and
zero new calls on restart. Then consume each published generation shard.

The coordinator accepts the matched-500 run as mechanically verified generation evidence and
waives only the unavailable historical clean-shutdown, process-absence, resource-claim,
resource-release, zero-call-replay, explicit-quality-acceptance, and fresh-download-publication
receipts. All 2,000 request terminals/keys, SSE bytes, extraction counts, and frozen
model/prompt/input identities remain mandatory.

## Shared 72-hour schedule

The local host has no active reservation at sprint start. It allows two Lean workers and 40 GiB
combined measured Lean RSS. Provider calls do not reserve Lean capacity. SFT2B generation runs on
the external eight-GPU host.

| Time | SFT1 | SFT2A | SFT2B |
| --- | --- | --- | --- |
| T+0–6h | compact engine, seven operations, fixtures | malformed path, universes, prompt, audit recovery | mechanical v3 build/verify/publish |
| T+6–12h | resumable batch runner | dynamic queue/oracle pool and 20-root pilot | compile all 1,242; consumer resume fixes |
| T+12–24h | 100 roots plus 30-row inspection | ledger fix; certify roots; launch shard 1 | 100-source kill/resume; launch generation shard 1 |
| T+24–48h | 10K, shortcut screen, private release | run remaining 1K shards | generation shards 2–4; downstream pilot |
| T+48–72h | full Mathlib wave shards if 10K passes | compact/publish final 10K artifacts | finish/publish full generation; consume published shards |

Lean-heavy work is interleaved: SFT1 compile/fixtures → SFT2B 1,242 audit → SFT2A 20-root pilot →
SFT1 100 roots. After that, allocate one persistent worker each to SFT1 and SFT2A when both need
Lean. Resource unavailability delays only the Lean stage; it does not block source work, provider
queues, GPU generation, compaction, or publication.

## Deferred, not deleted

- SFT1: full 46-operation conformance, all old authority/readiness loaders, P01 identity exception,
  composition, multi-project census/matrix, per-stage authorization text, and separate rubric/proof
  core lanes.
- SFT2A: per-attempt recovery configs and authorization receipts, full-tree hashing, 100% replay,
  code-file hashes in cache identity, exact mechanism retries, legacy rejudging, and four-slot
  batching.
- SFT2B: the remaining 991/992 review panel as a launch gate, fixed host/GPU literals, repeated
  model-snapshot hashing, full pilot replay on each launch, working-tree code pins in request IDs,
  and the waived historical receipts.

Keep frozen REPR and input/model/prompt identities, checked label evidence, gold/self/duplicate
screens, persistent workers, semantic caches, append-only terminals, exact cell coverage,
deterministic compaction, resource claims, and verified detached-run health.

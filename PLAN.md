# LeanFaith value-first parallel data plan

> **Status:** active; 72-hour SFT execution sprint from integrated local `main`
> **Approved:** 2026-08-30
> **Last updated:** 2026-09-01
> **Current scope:** prepare, validate, and publish the datasets and evaluation assets. Do not start
> full training from this coordinator task.

LeanFaith is building a learned judge of whether two Lean theorem statements express the same
mathematical claim. The immediate goal is to create high-value data quickly: very large, cheap
automatic supervision first, followed by smaller and richer LLM-assisted supervision. CPT and SFT
stages remain ablations; evaluation is mandatory.

The previous execution ledger is preserved at
[`docs/archive/PLAN-2026-08-30-refocus-v3.md`](docs/archive/PLAN-2026-08-30-refocus-v3.md).
It is evidence and reusable history, not the active task list.

## Non-negotiable operating rule

Lean is the bottleneck. Every task must do text, schema, provenance, filtering, and deduplication
without Lean when possible. When Lean is necessary, use a persistent environment, batch requests,
bounded parallel workers, content-addressed caches, and staged samples. Never launch one Lean
process per row or recompile an entire corpus by default. The full contract is in
[`plans/00_shared_contracts.md`](plans/00_shared_contracts.md).

## Parallel workstreams

| Workstream | Task brief | Current state | May start independently? |
| --- | --- | --- | --- |
| Shared goal representation | [`plans/02_goal_v1.md`](plans/02_goal_v1.md) | complete and frozen | downstream pin only |
| Existing-data reuse | [`plans/05_existing_data_reuse.md`](plans/05_existing_data_reuse.md) | complete | downstream consumption only |
| CPT phase 1 | [`plans/10_cpt1.md`](plans/10_cpt1.md) | complete and privately released | training ablation later |
| CPT phase 2 | [`plans/20_cpt2.md`](plans/20_cpt2.md) | complete and privately released | training ablation later |
| SFT phase 1 deterministic | [`plans/30_sft1_deterministic.md`](plans/30_sft1_deterministic.md) | active; compact proof-backed sprint engine next | 100 roots, then automatic 10K/full-wave progression on pass |
| SFT phase 2A LLM transforms | [`plans/40_sft2_llm_transforms.md`](plans/40_sft2_llm_transforms.md) | active; repair throughput/resume path without rerunning 100 roots | 20 roots, then sharded 10K on pass |
| SFT phase 2B autoformalization | [`plans/50_sft2_autoformalizer.md`](plans/50_sft2_autoformalizer.md) | active; corrected-core scale path | mechanical v3 + full pilot compile audit, then four generation shards |
| Evaluation and baselines | [`plans/60_eval_baselines.md`](plans/60_eval_baselines.md) | not started | yes; mandatory |
| Training and ablations | [`plans/70_training_ablations.md`](plans/70_training_ablations.md) | deferred | design/smokes only |

These streams are intentionally parallel. REPR freezes the shared `goal_v1.0` serializer while
CPT1, CPT2, DATA-REUSE, and the EVAL split work proceed. SFT/evaluation serialization depends on
REPR, but their source/prompt/baseline preparation can start. Existing-data reuse informs SFT1/SFT2
but does not need to finish before CPT or evaluation. SFT1 bulk generation has one hard user gate:
its owner must first review the complete preserving/breaking transform catalog with the user. Full
training waits for versioned datasets and frozen evaluation inputs, but architecture preparation
may proceed.

## 2026-09-01 integration checkpoint

The three SFT branches have been integrated locally into `main`; no remote `main` push is implied.
The source tips are SFT1 `8be4ef6`, SFT2A `42cd0d6`, and SFT2B `06df8c0`. The rejected
24K-line SFT1 v0.3.6 archive remains outside `main`.
At inspection time, no SFT1, SFT2A, or SFT2B process, detached `tmux` session, or shared host
reservation was active.

- **SFT1:** the exact two-row thin smoke produced one kernel-supported preserving P18 pair and one
  grounded N31 breaking pair, then replayed with two cache hits and zero Lean calls. This validates
  serialization, evidence, caching, and resource cleanup only. It does not validate the archived
  Wave 1 engine. Repair and compile the real engine under opened telescopes, exercise P15/P18/P21
  (and P01 if retained), and manually inspect 10--20 real rendered roots before any 100-root gate.
- **SFT2A:** the 100-root rehearsal completed all 400 requested slots and retained 284 pairs, with
  a zero-provider/zero-Lean replay. Its Kimi audit stopped on a judgment-schema mismatch, leaving
  one reservation unresolved and no final audit manifest. Repair and resume only that audit, then
  run a 20-root performance pilot with a dynamic provider queue and exactly two persistent Lean
  workers. Do not rerun the completed generation or authorize 10K/50K yet.
- **SFT2B:** the matched-500 ReForm-32B generation completed 2,000 requests on eight H100-80GB
  GPUs, admitting 1,242 contract-valid signatures (1,147 globally unique) in 924.6 seconds. This is
  generation evidence, not semantic-label evidence. Freeze corrected source v3 and compile a
  stratified 200-candidate sample with persistent Lean before authorizing the corrected 50K-source
  core; the full Lean plus three-judge consumer remains a separate scale gate.

The next three bounded tasks should proceed in parallel. Every subsequent long run must use a
named detached `tmux` session with durable journals, verified liveness, and a recorded resource
claim. Provider work should be parallelized independently of the two-worker Lean host cap; Lean
sessions should be persistent and project-grouped rather than recreated per root.

Clean-worktree verification passed 520 focused SFT1 tests and all 53 SFT2A tests. SFT2B passed
129 tests with four narrowly skipped historical-evidence replays because repository-ignored
raw/parsed evidence is not mounted in the integration worktree; those checks remain fail-closed
and executable when the evidence is present.

## 2026-09-01 72-hour SFT execution reset

The user accepted the substance of the independent GPT Pro and Claude Fable 5 reviews and asked
the project to stop spending time on authorization prose and repeated evidence checks. The active
cross-task execution contract is [`plans/72h_sft_data_sprint_2026-09-01.md`](plans/72h_sft_data_sprint_2026-09-01.md).
It supersedes older task-brief sequencing where that sequencing adds ceremony but does not protect
labels, resumability, or an expensive run. Historical plans, configs, receipts, and data remain
immutable evidence; they are frozen, not deleted or rewritten.

Passing the objective gates in the sprint plan authorizes the next named stage automatically. No
new exact authorization sentence, full-tree rehash, clean-worktree receipt, exhaustive registry
matrix, or repeated review is required. The remaining hard protections are: checked evidence for
every SFT1 label; Lean validity and semantic-judge routing for SFT2; self-pair/gold/duplicate screens;
and append-only terminals plus tested resume for every long run.

- **SFT1:** preserve the uncompiled historical Wave 1 stack. Build a compact seven-operation,
  single-hop Mathlib engine from the working thin-smoke route. Positives require a checked
  equivalence witness; negatives require a checked source proof and exact candidate refutation.
  Run one success and typed rejection fixture per operation, then 100 deterministic roots and a
  30-row inspection. A pass starts a sharded 10K run without another approval. Composition and P01
  remain off. Larger scale starts only if the 10K shortcut checks pass and measured throughput fits
  the remaining sprint window.
- **SFT2A:** do not rerun the completed 100-root generation. Fix malformed-provider terminals,
  universe/section-variable elaboration, persistent oracle reuse, dynamic provider scheduling,
  accepted-only deduplication, and quadratic ledgers. Keep independent per-slot Terra calls; use
  concurrency rather than a new four-slot envelope. A 20-root kill/resume pilot gates ten resumable
  1K-root shards; its first shard is the long-run throughput gate. Kimi is checkpointed audit
  telemetry and routes disagreements out of the audited core rather than serializing generation.
- **SFT2B:** build a boundary-preserving mechanical quarantine/refill release; no 991-row review
  delay. Compile every admitted matched-500 candidate, then prove a 100-source generation
  kill/resume after fixing semantic cache keys, retries, stale-process recovery, and the sliding
  request window. A pass launches the corrected 50K core as four independently publishable
  12.5K-source ReForm shards. Build persistent Lean plus concurrent three-judge consumption in
  parallel and start it as soon as shard 1 publishes.

For the matched-500 SFT2B run, the coordinator explicitly waives the historical clean-shutdown,
process-absence, resource-claim/release, zero-call-replay, quality-acceptance, and fresh-download
receipt prerequisites. The data-bearing request terminals, request keys, extraction results, and
observed pilot verification remain required. This waiver grants no permission to ignore a live
process, duplicate requests, source validity, or a failed resume test.

## Shared decisions

- Model-facing theorem pairs use the compact `goal_v1.0` representation: ordered locals and
  hypotheses followed by exactly one `⊢ target`. The declaration name, shell, imports, and proof
  are removed. CPT2 is the intentional exception and stores the statement/context prefix plus
  proof body.
- Minimal release schemas are `{text}` for CPT1, `{theorem, body, label}` for CPT2,
  `{reference, candidate, label}` for SFT, and
  `{pair_id, reference, candidate, label, split}` for evaluation. Rich metadata belongs in a
  sidecar and may enrich SFT2, but it must never block cheap SFT1 generation.
- The main equivalence datasets contain both valid equivalent and valid non-equivalent pairs.
  Syntactically invalid candidates are useful, but live in a separate validity/acceptability view.
- New dataset repositories are private-first under `Lemmy00`. Every release pins inputs and
  records code revision, schema version, counts, hashes, seeds, and provenance.
- All 5,111 canonical gold examples become evaluation-only: deterministic 2,555 validation and
  2,556 test, with no training split. The expert-human, non-conflict subset is the primary semantic
  headline; auto-typecheck labels are an auxiliary validity diagnostic. Baselines may be run and
  stored on both now; model selection uses validation and the final selected model is compared on
  the unchanged test set.
- Initial model family: Ettin-150M first, ModernBERT as the control. Encode the two sides
  separately, cross-match them with decoder-style cross-attention layers, then predict a binary
  probability. Other architecture families are deferred.
- Theorem statements are in scope now. Applying the same system to `def` declarations is a future
  extension.
- New SFT2A/SFT2B LLM work defaults to Claude Code Opus 5/high, Codex GPT-5.6 Terra/high, and
  Lemex Kimi 2.7/high. Cost is tracked and safety-capped but is not the model-selection objective;
  frozen earlier runs remain immutable and new settings use additive configs.

## Session and ownership model

The coordinator owns this file, [`plans/README.md`](plans/README.md), and cross-task decisions.
Each task session owns only its task brief and the code/config/test paths it explicitly claims in
that brief. A worker must not silently change another task's contract or root status. Shared-code
changes require a short compatibility note in both affected task logs.

Start a session by copying the kickoff prompt from the relevant task brief. Before implementation,
the session must record its owner, status, exact writable paths, first one-example smoke, and Lean
budget. Progress is recorded in that task brief, not by rewriting this coordinator plan.

## Delivery order

1. Complete one-example end-to-end smokes and freeze schemas/manifests.
2. Run bounded pilots with measured throughput and quality gates.
3. Ask the user for compute or policy decisions where the task brief says to stop.
4. Scale only after the pilot passes; keep every long job resumable and run unattended work in a
   verified named `tmux` session that survives the launching agent turn.
5. Publish data and manifests to the named private Hugging Face repository.
6. Run evaluation baselines on validation and test; later select models on validation only.

The setup index, status protocol, communication templates, and repository map are in
[`plans/README.md`](plans/README.md).

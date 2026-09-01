# Parallel task hub

This directory is the operational entrypoint for separate LeanFaith sessions. The active research
direction is in [`../PLAN.md`](../PLAN.md); shared schemas and cost rules are in
[`00_shared_contracts.md`](00_shared_contracts.md).

The current cross-task execution override is the
[`72-hour SFT data sprint`](72h_sft_data_sprint_2026-09-01.md). It removes repeated authorization
and historical-evidence gates while retaining label, validity, deduplication, and resume checks.

## How to start a task session

**Active SFT baseline:** create a dedicated task worktree/branch from coordinator commit
`c17104fe9bec1cb9eaf847c4e412aa0ca76c178a` on local `main` (or a later coordinator descendant).
Do not share the integration checkout and do not continue from a pre-integration task tip. Historical
setup baseline `3557009` remains useful only for archaeology; every new worktree retains the
plan-contract check.

1. Choose one task below and open its brief.
2. Copy the `Session kickoff prompt` from that file into a new Codex/Claude/Lemex session.
3. The new session updates only its own task header before implementation: owner, status, time,
   writable paths, next gate, and Lean budget.
4. Run the smallest still-unpassed gate in the task brief or 72-hour sprint. Do not repeat a passed
   pilot or stop for a new approval when the sprint already authorizes automatic progression.
5. Record progress and handoff details in the same file. Ask the coordinator/user about cross-task
   changes rather than editing another task's contract.

## Long-run handoff

Authorized work that will outlive the current agent turn runs in a named detached `tmux` session,
never in the foreground of the Codex/Claude/Lemex task. Use the repository runner's durable journal,
cache, and resume support in the style of
`/localhome/milikic/annotate_numina/run_reasoning_direct.py`; tmux provides process survival, while
the journal and manifests provide correctness.

Before leaving it alone, the launching agent records and verifies:

- committed code revision, config/run hash, resource claim, ceilings, and exact output/cache roots;
- tmux session name, pane PID, start time, persistent log and journal, and sanitized launch/resume
  commands;
- a live process tree, clean startup log, and at least one advancing durable count/artifact; and
- attach and read-only status commands for the next session.

After this check, leave the tmux job running. Later sessions inspect durable state and tmux liveness
without relaunching it. A vanished tmux session is investigated from its terminal marker/log; it is
not automatically success or permission to start a duplicate.

## Task map

| ID | Brief | Output | Dependency or stop point |
| --- | --- | --- | --- |
| REPR | [`02_goal_v1.md`](02_goal_v1.md) | `goal_v1.0` spec, renderer, and compile-context contract | shared prerequisite for SFT/eval serialization |
| DATA-REUSE | [`05_existing_data_reuse.md`](05_existing_data_reuse.md) | reusable-data inventory | read-only until inventory classifications are agreed |
| CPT1 | [`10_cpt1.md`](10_cpt1.md) | `Lemmy00/leanfaith-cpt1-v1` | no Lean required |
| CPT2 | [`20_cpt2.md`](20_cpt2.md) | `Lemmy00/leanfaith-cpt2-proof-validity-v1` | 500-row Lean oracle only for splitter choice |
| SFT1 | [`30_sft1_deterministic.md`](30_sft1_deterministic.md) | `Lemmy00/leanfaith-sft1-deterministic-v1` | compact seven-op 100-root diagnostic, then automatic 10K |
| SFT2A | [`40_sft2_llm_transforms.md`](40_sft2_llm_transforms.md) | `Lemmy00/leanfaith-sft2-llm-transforms-v1` | 20-root scheduler/resume pilot, then sharded 10K |
| SFT2B | [`50_sft2_autoformalizer.md`](50_sft2_autoformalizer.md) | `Lemmy00/leanfaith-sft2-autoformalizer-v1` | mechanical v3 + 1,242 compile audit + resume smoke, then full core |
| EVAL | [`60_eval_baselines.md`](60_eval_baselines.md) | `Lemmy00/leanfaith-eval-v2` and results | mandatory; 2,555/2,556 split |
| TRAIN | [`70_training_ablations.md`](70_training_ablations.md) | checkpoints/ablation report | deferred until datasets; smokes allowed |

## Concurrency boundaries

- REPR, CPT1, CPT2, DATA-REUSE, and the EVAL split can start immediately and independently.
- SFT1, SFT2A, and SFT2B implement and run their 72-hour sprint paths concurrently; Lean-heavy
  stages share the two-worker/40-GiB host cap, while provider and external-GPU work continues.
- Passing the sprint's objective task gate authorizes its next named shard/run. No additional exact
  sentence or external review is required.
- TRAIN may validate the existing cross-attention implementation, but it must not consume an
  unfrozen dataset or begin full training.
- Only the coordinator updates `../PLAN.md`, this index, shared contracts, or cross-task schemas.

## Shared host reservations

The initial machine-wide limit is two Lean workers and 40 GiB combined measured Lean RSS,
whichever is reached first. The implicit allocation is zero: a task must claim resources before it
starts Lean or uses the local 24 GiB RTX 4090. Claims live outside worktrees at
`/storage/milikic/leanfaith/value_first/host_reservations/` and are atomically checked by the shared
CLI, so separate branches see the same state:

```bash
uv run leanfaith-resources list
uv run leanfaith-resources claim CPT2 --workers 1 --lean-rss-gib 20
uv run leanfaith-resources release CPT2
```

Record the claim in the task log and release it after the job. The local host permits one GPU claim
at a time (`--gpu`). Task agents request increases or stale-claim cleanup in their own `Coordinator
requests` section; only the coordinator edits the summary table.

| Task | Lean workers | Lean RSS | GPU | Reservation state |
| --- | ---: | ---: | --- | --- |
| unassigned | 0 / 2 | 0 / 40 GiB | free | no active reservation |

## Communication format

Use this compact update in the task log and when messaging the user/coordinator:

```text
Task/status:
Completed evidence:
Current counts and paths:
Lean usage: calls, cache hit rate, throughput, projected remaining cost
Detached run: tmux session, pane PID, start time, log/journal, attach/status commands
Decision needed:
Next bounded action:
```

For compute escalation:

```text
COMPUTE REQUEST — <task>
Pilot hardware and measured throughput:
Projected rows/tokens and wall time:
Requested hardware (A100/H100/etc.) and estimated duration:
Checkpoint/journal path and resume command:
Why the local host is insufficient:
```

Use [`TASK_TEMPLATE.md`](TASK_TEMPLATE.md) when adding a future workstream. Do not copy an archived
plan forward wholesale.

## Independent readiness review

Claude Fable 5 reviewed the full setup twice at maximum reasoning. Its initial blockers and all
resolutions, followed by the clean-baseline **READY** verdict, are recorded in
[`reviews/2026-08-30-claude-fable5-setup-review.md`](reviews/2026-08-30-claude-fable5-setup-review.md).

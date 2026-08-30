# Parallel task hub

This directory is the operational entrypoint for separate LeanFaith sessions. The active research
direction is in [`../PLAN.md`](../PLAN.md); shared schemas and cost rules are in
[`00_shared_contracts.md`](00_shared_contracts.md).

## How to start a task session

1. Choose one task below and open its brief.
2. Copy the `Session kickoff prompt` from that file into a new Codex/Claude/Lemex session.
3. The new session updates only its own task header before implementation: owner, status, time,
   writable paths, next action, and Lean budget.
4. Run the one-example smoke. Do not scale until the documented pilot gate passes.
5. Record progress and handoff details in the same file. Ask the coordinator/user about cross-task
   changes rather than editing another task's contract.

## Task map

| ID | Brief | Output | Dependency or stop point |
| --- | --- | --- | --- |
| REPR | [`02_goal_v1.md`](02_goal_v1.md) | `goal_v1.0` spec, renderer, and compile-context contract | shared prerequisite for SFT/eval serialization |
| DATA-REUSE | [`05_existing_data_reuse.md`](05_existing_data_reuse.md) | reusable-data inventory | read-only until inventory classifications are agreed |
| CPT1 | [`10_cpt1.md`](10_cpt1.md) | `Lemmy00/leanfaith-cpt1-v1` | no Lean required |
| CPT2 | [`20_cpt2.md`](20_cpt2.md) | `Lemmy00/leanfaith-cpt2-proof-validity-v1` | 500-row Lean oracle only for splitter choice |
| SFT1 | [`30_sft1_deterministic.md`](30_sft1_deterministic.md) | `Lemmy00/leanfaith-sft1-deterministic-v1` | hard user approval after transform audit |
| SFT2A | [`40_sft2_llm_transforms.md`](40_sft2_llm_transforms.md) | `Lemmy00/leanfaith-sft2-llm-transforms-v1` | compile/judge each candidate; pilot first |
| SFT2B | [`50_sft2_autoformalizer.md`](50_sft2_autoformalizer.md) | `Lemmy00/leanfaith-sft2-autoformalizer-v1` | source mix + 500-source model pilot |
| EVAL | [`60_eval_baselines.md`](60_eval_baselines.md) | `Lemmy00/leanfaith-eval-v2` and results | mandatory; 2,555/2,556 split |
| TRAIN | [`70_training_ablations.md`](70_training_ablations.md) | checkpoints/ablation report | deferred until datasets; smokes allowed |

## Concurrency boundaries

- REPR, CPT1, CPT2, DATA-REUSE, and the EVAL split can start immediately and independently.
- SFT2A and SFT2B can prepare source/prompt/runner pilots concurrently.
- SFT1 may inspect code and produce the transform proposal, but it stops for explicit user review
  before bulk generation.
- TRAIN may validate the existing cross-attention implementation, but it must not consume an
  unfrozen dataset or begin full training.
- Only the coordinator updates `../PLAN.md`, this index, shared contracts, or cross-task schemas.

## Shared host reservations

The initial machine-wide limit is two Lean workers and 40 GiB combined measured Lean RSS,
whichever is reached first. Every task starts with one worker. The local 24 GiB RTX 4090 permits
one GPU job at a time. Task agents request reservations in their own `Coordinator requests`
section; only the coordinator edits this table.

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

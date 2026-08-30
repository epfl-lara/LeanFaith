# TASK-ID — short title

> **Task ID:** TASK-ID
> **Status:** not_started
> **Owner/session:** unassigned
> **Last updated:** YYYY-MM-DD
> **Dependencies:** none or exact prerequisite task/gate
> **Next gate:** one bounded, evidence-producing action
> **Compute class:** CPU/network/Lean/GPU/API as applicable
> **Lean budget:** zero or exact oracle/persistent-worker limit
> **Local staging root:** `/storage/milikic/leanfaith/value_first/task_v1/`
> **HF destination:** private `Lemmy00/leanfaith-task-v1` or none with reason

## Objective

State the concrete deliverable and why it has value.

## Scope and ownership

**In scope:**

- ...

**Out of scope:**

- ...

**Writable paths:**

- This task brief.
- Exact code/config/test/output paths claimed by the session before editing.

## Inputs and outputs

List pinned input revisions, local reusable artifacts, minimal row schema, sidecar schema, local
staging root, and exact private-first Hugging Face repository/configuration.

## Label contract

State exactly who or what creates the label, what `1`, `0`, and `unknown` mean, and where invalid
rows live. Never leave this implicit.

## Lean-efficiency plan

Lean is the bottleneck. State which work uses no Lean, the smallest oracle/audit requiring Lean,
the persistent worker and cache design, concurrency cap, retry policy, and throughput/compute gate.
Never compile one process per row or default to a corpus-wide recheck.

## Execution gates

### One-example smoke

Describe one input through final serialized output, manifest, and resume behavior.

### Pilot

Define sample size, acceptance thresholds, measured outputs, and stop conditions.

### Scale

Define target size, sharding/resume/compaction, audits, publication checks, and compute escalation.

## Acceptance criteria

- ...

## Session kickoff prompt

```text
Work only on TASK-ID in /localhome/milikic/LeanFaith. Read AGENTS.md, PLAN.md,
plans/00_shared_contracts.md, and this brief completely. Inspect the worktree and claim exact
writable paths in this brief before edits. Preserve unrelated work. Lean is the bottleneck: avoid
it where possible; otherwise use persistent batched workers, bounded parallelism, caching, and the
one-example/pilot gates. Do not start scale work without satisfying the brief. Update this brief's
status and append-only log as you work. Finish with evidence, durable paths, risks, and the next
bounded action.
```

## Coordinator requests

- None yet. Request any shared package/config/dependency/worker reservation here.

## Progress log (append-only)

- YYYY-MM-DD — setup created; no execution performed.

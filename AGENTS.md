# LeanFaith agent operating contract

This file applies to every Codex, Claude, Lemex, or human-assisted session in this repository.

## Read order

1. Read [`PLAN.md`](PLAN.md).
2. Read [`plans/00_shared_contracts.md`](plans/00_shared_contracts.md) completely.
3. Read exactly one task brief under `plans/` and claim that task before changing code.
4. Inspect the current worktree and preserve unrelated changes. In particular,
   `src/leanfaith/corpus2/from_mixed_v0.py` may be user work until it is explicitly claimed.

For independently launched sessions, prefer a separate git worktree and a `milikic/<task>` branch.
If sessions share one worktree, the ownership boundary below is mandatory, not advisory.

Historical plans, reports, frozen manifests, and content-addressed artifacts are evidence. They do
not override the active plan. Do not edit hash-bound historical outputs merely to reformat them.

## Lean is the bottleneck

Repeat this decision in every implementation plan and progress handoff:

- Do all safe string parsing, schema work, source filtering, provenance, joins, and deduplication
  before invoking Lean.
- Never compile a full corpus merely to increase confidence. Establish a small Lean oracle,
  measure the cheap method against it, and compile only the bounded audit required by the task.
- Reuse one initialized Lean/LeanInteract environment per project and toolchain. Send batches to
  persistent workers; never run `lake env lean` once per row.
- Keep higher-level code behind `leanfaith.lean.protocol`; only the central LeanInteract backend
  should import LeanInteract. External verifier scripts are patterns, not a second backend.
- Cache results by content hash plus Lean version, project/environment revision, imports/options,
  and checker version. Successful and deterministic terminal failures are reusable.
- Parallelize with bounded, isolated workers. Cap concurrency from measured memory/throughput, not
  CPU count alone. Keep per-worker temporary files and append-only journals separate.
- Coordinate the total worker budget across simultaneous tasks. Eight workers per task is not a
  safe machine-wide default. Use synchronous elaboration (`Elab.async=false`) for data production.
- Retry only infrastructure failures and timeouts. Do not repeatedly retry deterministic syntax or
  elaboration errors.
- Measure 1 row, then 100, then roughly 10K rows before scale. Record rows/second,
  startup time, cache hit rate, failure classes, RAM/VRAM, and projected wall time.
- For necessary large checks, reuse the persistent/batched patterns in
  `/localhome/milikic/rl_theorem_provers/src/data/solve_new_problems/evaluate_lean.py` and the
  repository's `src/leanfaith/lean/` cache/session utilities.
- Stop and request an A100/H100 environment when a measured pilot shows the local GPU or host is
  the limiting resource. Do not quietly launch a multi-day job.

## Task ownership and progress

- A task session edits only its own brief plus paths declared in its `Writable paths` section.
- Update `Status`, `Owner/session`, `Last updated`, `Next gate`, and the append-only progress log
  at meaningful boundaries.
- Allowed statuses are: `not_started`, `active`, `waiting_user`, `blocked`, `pilot_ready`,
  `pilot_passed`, `scale_authorized`, `scaling`, `complete`, and `deferred`.
- At most one status is active. `blocked` requires an external dependency; ordinary unfinished work
  remains `active`.
- Do not start bulk generation from a planning/review task. Complete the one-example smoke and
  pilot gate in the task brief first.
- The SFT1 owner may audit and propose transforms but must set `waiting_user` before bulk data
  creation until the user explicitly approves the catalog.

## Durable unattended runs

- Any authorized job expected to outlive the current interactive turn runs in a named detached
  `tmux` session. Do not leave a scale job attached to an agent shell, rely on the desktop task
  remaining open, or substitute an untracked background process.
- Launch only after the brief records the exact command/config hash, committed code revision,
  output and cache roots, append-only journal/log paths, resource reservation, ceilings, resume
  command, and stop conditions. Never put tokens or other secrets in the tmux command line.
- Use a task/run-specific name such as `leanfaith-<task>-<run-id>`. Redirect stdout and stderr to a
  persistent log, keep stdin closed for background Codex/Lemex workers, and make the runner write a
  durable terminal status or completion marker. Cleanup must release task-owned resource claims
  only after the real job terminates.
- Before handing off, prove that the detached job is healthy: verify the tmux session and pane PID,
  inspect the process tree and initial log, and observe the journal/output advancing without a
  deterministic startup failure. Record the session name, pane PID, start time, attach command,
  read-only status/tail command, and the first durable counts in the task brief.
- Once the startup check passes, leave the tmux session running. A later agent monitors durable
  artifacts and session liveness without restarting or duplicating the job. Completion still
  requires manifests, hashes, counts, and release of reservations; a missing tmux session alone is
  neither success nor permission to relaunch.

## Implementation and cleanup

- Prefer extending existing modules over creating parallel replacements. Search the repo and the
  existing-data inventory before writing a new runner.
- New task code goes in the disjoint package/config/test paths in its brief. `src/leanfaith/lean/`,
  existing `src/leanfaith/generation/` and `collect2/`, `pyproject.toml`, `uv.lock`,
  `.pre-commit-config.yaml`, `configs/projects/`, root policies, and shared plans are coordinator
  owned. Request changes in the task's `Coordinator requests` section.
- Every new task-owned test directory contains an `__init__.py`. The repository also uses pytest's
  importlib mode so identical test basenames in different task directories do not collide.
- Delete or archive code only after proving it is superseded, finding all references, and running
  the relevant tests. Never delete frozen evidence or unrelated user work.
- Keep release code deterministic, restartable, and configuration-driven. Long jobs require an
  append-only journal, stable row IDs, atomic shard completion, and deterministic compaction.
- Never mix raw provider output, invalid candidates, unknown judgments, and core labeled training
  rows in one ambiguous split. Use named configurations/views.
- Test one complete example first, including the final serialized row and manifest entry. Then test
  resume behavior and duplicate suppression before any pilot.
- Use `rg` for repository search. Codex sessions use `apply_patch`; other agents use an equally
  precise patch/edit operation. Never print tokens or source shell files with tracing enabled.
- Run non-mutating checks before formatters/fixers. Scope any autofix to owned paths so another
  session's files are not rewritten.

## Data and release rules

- Pin every Hugging Face input revision. Record selected subset/split and input file hashes when
  available.
- Publish private-first to the exact `Lemmy00` repository named in the task brief. Stage locally,
  validate schema/counts/checksums, then upload. A successful upload is not complete until the
  committed Hub revision and local manifest agree.
- `HF_TOKEN` may be loaded from the user's environment (for example after sourcing `~/.bashrc`),
  but must never be printed, copied into configs, or exposed to subprocess logs.
- Do not send private/internal source text to external LLM APIs unless the task brief explicitly
  authorizes that source. Preserve source URL, revision, license/redistribution note, and lineage.
- Training rows remain minimal. Store expensive explanations, vote traces, validity results, and
  relation labels in a keyed sidecar so they can be ablated without regenerating core pairs.

## Verification and handoff

Before handoff, run the narrow tests for changed code, formatting/static checks for touched Python,
and the task's one-example or pilot check. Report commands, pass/fail counts, untested risks, cache
location, output paths, Hub revision if published, and the exact next action. Never report a job as
complete based only on a progress bar or process exit; verify durable rows, manifests, and hashes.

## Operational gotchas

- In a shared checkout use `uv sync --group dev --group local-inference`; plain `uv sync` can prune
  Torch/Transformers needed by another task.
- Background `codex exec` and `lemex exec` calls receive closed stdin (`</dev/null`) or they can
  hang indefinitely. Use the structured, journaled wrappers in the task package.
- Existing Lean runners that expose `--memory-hard-limit-mb` use 24576 rather than a smaller hard
  address-space limit; lower limits have prevented Lean threads from starting. This does not replace
  the two-worker/40-GiB measured-RSS host cap.
- Historical `leanfaith-eval` v1 enforces the old final-test seal. EVAL v2 owns an additive path
  implementing the approved both-splits-now policy; do not bypass or mutate frozen v1 artifacts.
- The implicit host allocation is zero. Before Lean or local-GPU work, claim shared resources with
  `uv run leanfaith-resources claim <TASK> ...`; inspect/release with the same CLI. The atomic state
  is outside git at `/storage/milikic/leanfaith/value_first/host_reservations/`, so all worktrees
  share the two-worker/40-GiB/one-GPU cap.

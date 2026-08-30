# Claude instructions for LeanFaith

Follow [`AGENTS.md`](AGENTS.md), the active [`PLAN.md`](PLAN.md),
[`plans/00_shared_contracts.md`](plans/00_shared_contracts.md), and one claimed task brief. The task
brief is the authority for scope and outputs; archived plans are context only.

Do not run the whole program from one session. Work on one bounded task, record progress in its own
brief, preserve unrelated changes, and do not rewrite the root plan unless explicitly acting as the
coordinator.

Lean is the cost bottleneck. Avoid it for parsing/filtering/schema work. If Lean is required, first
make a one-example oracle, then use persistent batched workers, bounded parallelism, and a cache
keyed by input plus Lean/project/checker revisions. Do not compile a corpus row-by-row or launch a
large job before the task's pilot gate.

For review-only sessions, do not edit. Prioritize contract gaps, semantic-label leakage, accidental
mass compilation, non-resumable work, split contamination, concurrent file ownership, and missing
acceptance evidence. Give actionable findings with file paths and severity.

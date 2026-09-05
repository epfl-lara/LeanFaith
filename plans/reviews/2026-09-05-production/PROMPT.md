Review `epfl-lara/LeanFaith`, branch `milikic/sft1-production-review-20260905`.
Pin your review to the commit supplied with this prompt and report that commit in your answer.

Start with `plans/reviews/2026-09-05-production/README.md`. It gives the reading order,
current implementation references, captured evidence, and a Git-only replay. All required
review inputs are in Git. Absolute paths inside historical receipts identify their origins;
you do not need access to that machine or private Hugging Face datasets.

My priority is substantial, useful SFT1 training data as soon as possible, followed by training.
We have spent too long cycling through planning, testing, and failures. I want you to determine
which specific changes unlock production while keeping valid labels, reproducibility, and a
meaningful evaluation. Challenge the diagnosis rather than just endorsing it.

The current audit reports:

- 13,984 published SFT1 core pairs, with a simple relation-symbol shortcut scoring 96.3%.
- 508,600 separately published, easy N19 auxiliary pairs; these are not broad core coverage.
- Completed Wave 4 inputs from 202 theorem roots. Cross-run duplicate handling fails; a
  whole-group deduplication simulation retains 1,161 pairs/all 202 roots, but the existing
  balancing rule then retains only 60 pairs/15 roots, all Mathlib.
- A completed inventory of 1,745,040 compiler-source contextual signatures. The bounded audit
  crashed because descriptor enumeration expands combinations before applying its five-variant
  cap. At least one source also has an unresolved context, while the audit requires every
  sampled source to pass.

Verify these claims using the code and supplied evidence. Distinguish reproduced results,
recorded observations, and hypotheses. The evidence projection supports selection analysis;
it does not replay Lean certificates or verify an entire released corpus.

Focus on four decisions:

1. How should complete certified groups, unique stored pairs, training sampling/weights, and
   shortcut diagnostics interact? Check the shared-base arithmetic and whether the balancing
   gate defeats composition. Propose an implementable replacement without hiding shortcut risk.
2. How should cross-operation duplicate pairs be resolved while preserving complete certificates,
   stable identities, provenance, and deterministic resume?
3. Where should work/memory limits apply during enumeration and subsequent certificate
   reconstruction? Specify what cached results remain reusable and which cases require rerunning.
4. How should source compatibility/coverage be separated from the requirement that every
   retained source proof and every retained pair's certificate pass? Specify legitimate
   exclusions, terminal accounting, and conditions that must actually stop production.

Return:

- A clear verdict on the proposed production correction and any genuine blockers it misses.
- Exact files/functions to change, ranked by their impact on delivering data.
- Concrete acceptance rules and the smallest necessary regression for each critical change.
- A practical next-24–48-hours plan: reusable artifacts, tasks that can run in parallel,
  incremental publication milestones, and a first-training checkpoint. Qualify timing by
  measured throughput; distinguish eligible sources, generated pairs, and released pairs.
- Only the user/compute decisions that are genuinely necessary. Existing automatic progression
  and scale authorizations are recorded in the active SFT1 brief; do not reopen historical gates.

Lean is the bottleneck. Do filtering, deduplication, and selection cheaply; reuse persistent
workers and compatible caches; do not propose corpus-wide recompilation or repeat passed pilots.
Keep exact evidence for released labels, evaluation contamination screening, complete groups,
durable journals, and frozen releases intact. New optional transformations, SFT2 progress, and
extra baseline experiments should not delay viable SFT1 production. Identify any important
scientific weakness even if it requires revising this proposal, but avoid adding ceremony that
does not protect data quality or prevent an expensive failure.

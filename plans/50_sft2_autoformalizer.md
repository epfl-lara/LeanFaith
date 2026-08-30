# SFT2B — autoformalization consistency data

> **Task ID:** SFT2B
> **Status:** not_started
> **Owner/session:** unassigned
> **Last updated:** 2026-08-30
> **Dependencies:** REPR `goal_v1.0`; source-quality audit and frozen consistency/voting prompts
> **Next gate:** validate the existing 301-row pilot through compile-and-vote, then compare formalizers
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

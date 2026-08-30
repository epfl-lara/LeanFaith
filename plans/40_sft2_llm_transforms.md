# SFT2A — LLM-generated semantic transformations

> **Task ID:** SFT2A
> **Status:** not_started
> **Owner/session:** unassigned
> **Last updated:** 2026-08-30
> **Dependencies:** REPR `goal_v1.0`; shared rubric; roots may be selected independently of SFT1
> **Next gate:** one root, four candidate slots, compilation, Claude judgment, and independent retention
> **Compute class:** external LLM/API plus CPU/RAM for Lean; large run may need explicit budget approval
> **Lean budget:** compile each novel candidate once through cached persistent workers
> **Local staging root:** `/storage/milikic/leanfaith/value_first/sft2_llm_transforms_v1/`
> **HF destination:** private `Lemmy00/leanfaith-sft2-llm-transforms-v1`

## Objective and scale

Select 50K diverse theorem roots and ask Codex for two meaning-preserving and two meaning-breaking
Lean statement transformations per root. Compile every candidate, then use Claude as an independent
semantic judge. Retain each successful candidate independently; quality is more important than
forcing exactly four accepted rows per root.

## Root sources and active authorization

Sample theorem/lemma roots from Mathlib, Physlib, CSLib, and valid theorem-shaped rows of
`formalmathatepfl/compiler_data`, with a source/domain/signature-structure freeze in the pilot.
Mathlib/Physlib/CSLib use the pinned contexts in `configs/projects/{mathlib,physlib,cslib}.yaml`.
Compiler-data roots retain their source imports/context and are skipped when a safe project context
cannot be established; never guess one or compile the full source corpus.

External-model processing of `formalmathatepfl/*` is explicitly authorized by
`policies/source_use_v2.yaml`. This task creates additive v2 source configs under its own config
directory with `policy_version: source_use_v2` and `external_transmission: true`; it never edits the
fail-closed v1 source configs used by historical runs.

## Label workflow

For each root and candidate slot:

1. Provide Codex the `goal_v1.0` reference, raw compile context needed to write a valid statement,
   the requested polarity, and the consistency rubric from `00_shared_contracts.md`.
2. Require an informative transformation, not cosmetic churn. Ask the proposer to identify the
   intended semantic mechanism and likely trap for a learned judge.
3. Compile/elaborate the candidate once. Store the verdict. Invalid candidates are not semantic
   negatives; they remain in the auxiliary validity/attempt sidecar and the slot may be retried.
4. Give Claude a blinded reference/candidate pair and the full consistency rubric. Do not reveal
   the requested label. Require equivalent, non-equivalent, or unknown with a short structured
   rationale and relation/error type.
5. If Claude agrees with the requested polarity, retain the compiled pair with that binary label.
   If it disagrees or returns unknown, retry that slot up to three total attempts.
6. Preserve every accepted candidate even when sibling slots fail. After three failed attempts,
   mark only that slot unresolved; do not discard the root or accepted siblings.

The core schema is `{reference, candidate, label}` in `goal_v1.0`. A keyed sidecar stores proposer
model/version/effort, prompt hash, requested polarity, generation attempt, compilation cache key,
Claude model/version/effort, vote/rationale, relation class, raw references, and lineage.

For new generated rows, the user's “double agreement” is proposer-requested polarity plus one
blinded Claude judgment; record the precise basis as `proposer_intent+single_judge`, not two
independent semantic judges. Send a blinded 10% stratified sample to Lemex as a second-judge audit.
Systematic disagreement blocks scale; audited disagreements route to unknown/review unless resolved.

## Prompt requirements

Both prompts must define same intended mathematical claim carefully: equivalent is stricter than
both statements being true or sharing concepts; one-way implication is insufficient. Cover binder
types/domains, dependent quantifier order, assumptions and conclusion strength, coercions,
existence/uniqueness, boundary cases, converses, negation scope, and representation-only changes.
Ask for high-information hard cases across multiple mechanisms and reject output that merely
renames a theorem or makes a trivial whitespace change.

Freeze prompt versions and structured output schemas after adversarial one-root examples. The
production Claude model/effort and Codex model/effort must be pinned in the run manifest after a
quality/cost pilot; the separate repository-setup review does not choose those settings.

## Existing assets and reuse

- Use the resumable runner style in
  `/localhome/milikic/annotate_numina/run_reasoning_direct.py`: isolated workers, locked/append-only
  output, stable IDs, bounded retries, and deterministic final compaction.
- Reuse the repository's `collect2/`, provider invocation, prompt schema, Lean backend/cache, and
  journal components where contracts fit; do not create a second ad hoc executor.
- Import the 13,373 compiled Qwen/Kimi + Codex-judged records as a
  `legacy_single_judge` tranche after DATA-REUSE confirms hashes/schema:
  `/storage/milikic/leanfaith/corpus2/recovered_singlepass_codex_v1_e8567ba/outputs/trainer_records.jsonl`.
- For high-quality double agreement, send all 307 legacy positives, all unresolved records, and at
  least 2K stratified negatives to Claude. Accepted double-judged rows may join the core; keep the
  rest explicitly legacy rather than pretending they have two judges.

## Scope and ownership

**In scope:** root sampling, prompt/rubric design, provider wrappers, per-candidate compilation and
judgment, retries, legacy rejudging, core/sidecar serialization, manifests, and private publication.

**Out of scope:** deterministic SFT1 generation, autoformalizer NL translation, model training,
using invalid Lean as label `0`, or discarding accepted candidates due to incomplete siblings.

**Writable paths:** this brief; `src/leanfaith/sft2a/`; `configs/sft2a/`; `prompts/sft2a/`;
`tests/unit/sft2a/`; the staging root. Existing `collect2/`, generation/providers, corpus2, shared
Lean/project/dependency paths, and other tasks are read-only reusable interfaces; request
coordinator changes.

## Lean-efficiency plan

Lean is the bottleneck, but candidate compilation is required here. Run cheap schema/placeholder/
declaration-shape checks first. Group homogeneous projects/imports, prepare each environment once,
batch through persistent LeanInteract workers, and cache every terminal verdict by candidate,
context, Lean/project, options, and checker version. Reuse the verdict for goal rendering and all
judges; never compile separately for each vote. Retry only infrastructure failures.

Start with one worker and measure 4/8 under concurrent-session load. Keep `Elab.async=false`,
bounded batches, isolated requests, ordered results, and append-only attempt journals. Ask for CPU/
RAM capacity—not GPU—if Lean dominates; ask for larger compute/API budget if model inference does.

## Execution gates

### One-example smoke

One root must exercise four independent slots, structured parsing, compile cache, blinded Claude
judgment, accepted-sibling preservation, a rejected-slot retry, `goal_v1.0` rendering, final core row,
sidecar, journal, and restart without duplicate calls.

### Pilot

Run a diverse bounded root sample and the legacy double-judge sample. Report accepted rows/slot,
polarity balance, relation diversity, invalid/unknown/disagreement/retry rates, judge agreement,
Lean cache and throughput, LLM cost/latency, duplicate rate, and projected 50K-root cost. Review a
stratified human-readable sample before scale.

### Scale and publication

Scale only with explicit recorded model/prompt/budget settings. Use resumable root/slot shards and
deterministic compaction. Publish core, legacy, invalid-attempt, and rich-judgment configurations or
sidecars with unambiguous names. Screen both reference/candidate signatures against
`data/benchmarks/golden_blocklist_v1.json` using exact and near-duplicate keys; exclude hits from
training. Validate hashes/counts and the remote Hub revision.

## Acceptance criteria

- Candidates are compiled and judged independently under a frozen, careful consistency rubric.
- Accepted candidates survive sibling failures; retries are capped at three per slot.
- Invalid candidates are auxiliary, while valid non-equivalent candidates are core negatives.
- New core rows record proposer-intent + blinded-judge agreement and a 10% second-judge audit;
  legacy single-judge rows are visibly separate.
- All calls/checks are resumable, cached, versioned, costed, and reproducible.
- 50K-root scale is not started until the pilot quality/cost report is accepted.

## Session kickoff prompt

```text
Own only SFT2A in /localhome/milikic/LeanFaith. Read AGENTS.md, PLAN.md,
plans/00_shared_contracts.md, and plans/40_sft2_llm_transforms.md completely. Update this brief and
claim exact paths. Design and freeze careful Codex proposer and blinded Claude judge prompts, then
run exactly one root through four independent slots before any pilot. Preserve every accepted slot;
retry only its rejected slot up to three times. Lean is the bottleneck: compile each candidate once
through persistent cached workers and reuse the result. Invalid is not semantic false. Keep core,
legacy, unknown, and invalid views distinct. Do not launch the 50K run without a measured pilot and
recorded budget/model decision.
```

## Coordinator requests

- Approve pinned production proposer/judge settings and projected API budget after the pilot.

## Progress log (append-only)

- 2026-08-30 — task brief created; no LLM or Lean calls made.

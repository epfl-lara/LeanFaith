# Shared data, label, and execution contracts

> **Owner:** coordinator
> **Applies to:** every active task
> **Change rule:** task sessions propose shared-contract changes; only the coordinator merges them

## 1. Value/cost policy

The project optimizes useful supervision per unit of compute and human/model judgment.

- SFT1 is deliberately huge, cheap, automatic, and somewhat noisy. Its value comes from scale and
  known transform polarity. It must not require an LLM or a fresh Lean process/compilation for
  every pair; bounded persistent Meta workers may batch-check typed certificates for retained rows.
- SFT2 is smaller, more expensive, and higher quality. Compilation, multiple judges, explanations,
  relation labels, or auxiliary heads are appropriate there.
- Rich metadata is additive. Failure to produce it must not block a valid minimal training row.
- CPT1, CPT2, SFT1, and SFT2 are optional experimental stages. Evaluation is mandatory. Keep each
  stage as a separate versioned dataset so later ablations can skip, reorder, or merge stages.

### SFT2 LLM defaults

For new SFT2A and SFT2B transformation, judging, and labeling runs, the active default provider
settings are:

- Claude Code: Opus 5 through model alias `opus`, reasoning effort `high`;
- Codex: `gpt-5.6-terra`, reasoning effort `high`; and
- Lemex: `moonshotai/Kimi-K2.7-Code`, reasoning effort `high`.

The machine-readable contract is `policies/sft2_llm_labeling_defaults_v1.yaml`. These are defaults,
not retroactive edits: frozen smoke/pilot configs and their manifests remain byte-identical, and a
new run adopts the defaults through an additive config. Any deviation records its reason and exact
provider/model/effort pin.

Dollar cost is not a primary optimization or model-selection criterion for this research. Continue
recording usage and reported spend, and retain provider-call/token/spend ceilings as runaway-job
safety controls. Effort `high` is chosen to reduce unnecessary reasoning tokens relative to
`max`/`xhigh` while preserving strong labeling quality; value and correctness remain the decision
criteria.

## 2. Model-facing theorem representation

The initial representation is versioned as `goal_v1.0`, modeled after the useful goal view in
ConsistencyCheck and owned by [`02_goal_v1.md`](02_goal_v1.md):

```text
x y : ℝ
h : x < y
⊢ x ≤ y
```

It contains ordered local variables, hypotheses, typeclass/universe locals, and exactly one
turnstile target. Preserve local order, local names, dependent types, generated instance names,
coercions, notation, and universes. Remove the declaration keyword/name, attributes, command shell,
imports, options, comments, `:=`, `by`, `sorry`, and proof body. Do not alpha-normalize the model
text initially; a typed/alpha-normalized fingerprint may be stored separately for deduplication.

Filter theorem/lemma declarations before rendering because the goal text itself loses declaration
kind. Prefer elaborated rendering when an environment is already loaded or compilation is already
required; otherwise use the frozen surface renderer rather than triggering mass compilation. Store
`goal_v1_source: elaborated|surface` and report coverage/metrics by mode. Ambiguous surface rows may
be skipped. For already elaborated libraries, render types from one loaded environment instead of
recompiling proofs.

`goal_v1.0` is not a compilable source language. Sidecars retain the raw statement/source plus a
`compile_context` identifier covering project/toolchain revision, imports, namespaces/scopes, and
options. Candidate workflows compile their raw generated signature/declaration and only then render
the model view; they never reconstruct a declaration from goal text.

CPT2 is the intentional exception: it stores the cheap source prefix through the terminal `:=`
as `theorem` and the suffix after the selected `by` as `body`, because proof validity is the task.

## 3. Minimal schemas

Core training configurations contain only what the trainer needs:

```text
CPT1:  text: string
CPT2:  theorem: string, body: string, label: bool
SFT:   reference: string, candidate: string, label: bool
EVAL:  pair_id: string, reference: string, candidate: string, label: bool, split: string
```

Every release also has a manifest with schema version, source revisions/hashes, extraction code
revision, selection rules, counts, exclusions, seed, shard hashes, and Hub commit. Optional rich
fields live in a row-ID-keyed sidecar: source provenance, `label_provenance`, representation mode,
compile context, transformation chain, compilation result, judge votes/rationales, relation class,
validity, confidence, or faithfulness dimensions.

Stable IDs are hashes of canonical source identity plus deterministic operation/candidate slot,
not row numbers. Preserve raw strings before normalization in provenance or source shards when
allowed. Deduplicate deterministically and record before/after counts.

### Gold-contamination screen

Every training release screens against the existing 5,111-row benchmark blocklist at
`data/benchmarks/golden_blocklist_v1.json` and records exact input hash, match counts, and action.
SFT releases screen both sides with exact and existing signature-near-duplicate keys; matched
groups do not enter training. CPT1 `text` and CPT2 `theorem` at minimum receive an exact-string/hash
screen without Lean and report hits; CPT rows may be excluded or placed in a contamination-marked
configuration according to the task brief, but never silently mixed into headline training. The v1
blocklist remains applicable because EVAL v2 repartitions the same 5,111 canonical rows.

## 4. Label contracts

- **CPT2:** the source dataset's existing `isValid` is the label. Do not relabel or recompile all
  rows. The task is extraction, not a new validity audit.
- **SFT1:** no per-pair LLM judge. A preserving row is label `1` only when the exact typed
  transformation certificate and a checked equivalence witness replay for the closed pair. A
  breaking row is label `0` only when a checked separator or candidate refutation establishes
  non-equivalence for that exact closed pair. A rubric mutation, failed search, non-definitional
  equality, or expected polarity alone is diagnostic sidecar evidence and cannot enter the core.
  Historical N-RUBRIC/N-PROOF artifacts remain immutable, but new sprint data uses one operational
  rule: certified negative or no row. Composition remains disabled until single-hop coverage and
  shortcut diagnostics justify it.
- **SFT2A:** Codex proposes two preserving and two breaking candidates per root. Each candidate is
  compiled, then Claude independently judges intended-claim consistency. Retain every accepted
  candidate independently; a failed sibling does not discard it. Retry a failed slot at most three
  times. The core basis is precisely `proposer_intent+single_judge`, with the blinded second-judge
  audit defined in the SFT2A brief; do not describe it as two independent semantic judgments. The
  stored binary label comes from the accepted Claude verdict, not from the proposer-requested
  polarity; disagreement, malformed output, and unknown stay outside the binary core.
- **SFT2B:** an autoformalizer proposes four Lean candidates from an NL source with a trusted Lean
  reference. Compile each candidate. Codex, Lemex, and Claude vote under the shared rubric: at least
  two equivalent votes gives `1`, at least two non-equivalent votes gives `0`, otherwise unknown.
- **Evaluation:** preserve the human/canonical label. Never overwrite gold with a model judgment.
  `expert_human` non-conflict rows define the primary semantic headline. Auto-typecheck-failure
  labels remain an `auxiliary_validity` diagnostic and never become semantic training labels.

Valid non-equivalent candidates are core negative examples. Invalid/unelaborated candidates remain
valuable for a separate `validity_or_acceptability` configuration but are excluded from the
signature-only equivalence view. Unknowns remain in auditable sidecars, not binary training rows.

## 5. Consistency rubric for judges and prompts

Label equivalent only when the two formal statements express the same intended mathematical claim
under ordinary mathematical reading, not merely when both are true, share vocabulary, compile, or
are related by one implication. Preserve quantifier scope/order when dependent, binder domains and
types, hypotheses, conclusion strength, equality direction where relevant, existence/uniqueness,
edge cases, and required typeclass assumptions.

Representation changes, harmless alpha-renaming, logically reversible restatements, and explicit
versus implicit equivalent assumptions may be equivalent. Strengthened/weakened premises or
conclusions, lost domain guards, changed quantifiers/types, converse/negation mistakes, witness
dependency loss, or an unrelated true theorem are non-equivalent. Judges return `unknown` when
ambiguity, missing context, invalid Lean, or insufficient confidence prevents a reliable decision.
Compilation establishes syntactic/elaboration validity, never semantic equivalence.

Prompts must define these rules directly and request challenging, informative transformations
rather than superficial lexical changes. Preserve structured votes and short rationales in the
SFT2 sidecar for later auxiliary-head experiments.

## 6. Lean-efficiency contract

Lean is the bottleneck.

1. Build the non-Lean path first: text parsing, masked delimiter scanning, schemas, joins,
   filtering, source sampling, hashes, and manifests.
2. Establish a bounded Lean oracle only where needed. Compare cheap extraction/transformation
   against it and freeze the cheap method when thresholds pass.
3. Initialize each Lean project/toolchain once per worker. Batch many requests through persistent
   LeanInteract or file-backed drivers; never spawn `lake env lean` per example.
4. Cache by `(input/content hash, Lean version, project revision, import/options fingerprint,
   checker/renderer version)`. Write cache records and journals atomically.
5. Use bounded parallel workers with isolated temporary state. Determine the cap from a 1/100/10K
   throughput and memory study. Do not let workers multiply full Mathlib environments blindly.
6. Reuse successes and deterministic terminal failures. Retry only timeouts, crashed workers, and
   transient infrastructure errors with a bounded retry count.
7. Compile only the task's sample or candidate set. A full-corpus verification requires an explicit
   measured justification and user/compute approval.
8. Every long run is resumable: stable input IDs, append-only attempt journal, terminal-state
   index, atomic shard marker, deterministic merge, and duplicate suppression.

### Detached long-run contract

After an authorized pilot/scale setup passes its brief and startup checks, any run expected to
continue beyond the current agent turn must execute in a named detached `tmux` session. The job may
not depend on an interactive Codex/Claude/Lemex session staying open.

Before detaching, bind the run to committed code, a config/run hash, immutable inputs, ceilings,
resource claims, output/cache roots, an append-only journal, a persistent combined log, a resume
command, and explicit stop conditions. Keep credentials out of process arguments and logs; close
stdin for background Codex/Lemex subprocesses. A wrapper or runner records terminal status and
performs task-owned resource cleanup only when the underlying job actually exits.

The launching agent must verify the tmux session, pane PID/process tree, initial log, and at least
one advancing durable counter/artifact before handoff. The brief records the session name, start
time, pane PID, attach/status commands, first counts, and recovery command. After that health check,
leave the session running unattended. Monitoring is read-only and must not spawn a duplicate run.
When the session disappears, decide success from durable completion markers, manifests, hashes,
and counts—not from tmux state alone.

On the current shared host, the coordinator permits at most two concurrent Lean workers and 40 GiB
combined measured Lean RSS, whichever limit is reached first. The implicit allocation is zero.
Before starting Lean, a task atomically claims its measured workers/RSS through
`leanfaith-resources`; before local model work it claims the one 24 GiB RTX 4090. Claims live at the
shared out-of-repo root `/storage/milikic/leanfaith/value_first/host_reservations/`, so separate
worktrees observe the same totals. Release claims after the job; GPU capacity does not accelerate
Lean elaboration.

Use `/localhome/milikic/rl_theorem_provers/src/data/solve_new_problems/evaluate_lean.py` as a
reference for pooled verification and the repository's `src/leanfaith/lean/` modules for cache,
session, backend, and project-version patterns. Do not copy runners blindly; preserve the task's
minimal output contract.

## 7. Scale and compute gates

Each task must pass the smallest task-specific version of:

- **one example:** final serialized row, sidecar/manifest link, cache behavior, and resume behavior;
- **small pilot:** correctness/coverage thresholds plus measured rows/s and failure taxonomy;
- **about 10K rows or task-specific equivalent:** reliable wall-time/space projection;
- **scale:** only after the brief's explicit acceptance gate.

These are scientific and operational gates, not a requirement for a new exact authorization
sentence at every transition. A coordinator decision may authorize automatic progression from a
passing bounded gate to the next named shard/run. The task still records the measured result,
resource claim, durable launch contract, and stop conditions before starting; it does not pause for
another review when the already-recorded conditions pass.

The local RTX 4090/server is for correctness, parsing, baseline CPU work, and short throughput
experiments. Ask the user explicitly for A100/H100 capacity before large model inference/training or
when the measured projection is unreasonable. Include hardware, duration, checkpoint/journal, and
resume instructions in the request.

## 8. Hugging Face release contract

Use private repositories under `Lemmy00` until redistribution and quality are confirmed. Load
credentials from the environment without printing them. Before upload, validate schema, split and
label counts, nulls, IDs, duplicate rate, sample rows, provenance, file checksums, and deterministic
rebuild metadata. After upload, record the exact Hub commit and verify the remote schema/counts.

Planned repositories:

- `Lemmy00/leanfaith-cpt1-v1`
- `Lemmy00/leanfaith-cpt2-proof-validity-v1`
- `Lemmy00/leanfaith-sft1-deterministic-v1`
- `Lemmy00/leanfaith-sft2-llm-transforms-v1`
- `Lemmy00/leanfaith-sft2-autoformalizer-v1`
- `Lemmy00/leanfaith-eval-v2`
- `Lemmy00/leanfaith-eval-results-v2`

`policies/source_use_v2.yaml` records the owner's active research/external-model authorization for
`formalmathatepfl/*`; `policies/evaluation_use_v2.yaml` records the requested LLM-baseline use of
the canonical evaluation pairs. Active tasks create additive v2 source configs and never weaken or
edit hash-bound v1 configs used to replay historical runs.

## 9. Code and session boundaries

Task owners claim exact writable paths before edits. New work uses disjoint task packages/config/
test directories named in each brief. The following are coordinator-owned shared surfaces:
`src/leanfaith/lean/`, existing `src/leanfaith/generation/`, existing `src/leanfaith/collect2/`,
`pyproject.toml`, `uv.lock`, `.pre-commit-config.yaml`, `configs/projects/`, this file, and root
plans/policies. A task requests changes to them in its own `Coordinator requests` section.

Reuse existing modules where sensible through stable interfaces; record each asset as `reuse`,
`adapt behind task package`, `legacy reference`, or `retire after proof`. Never delete frozen
evidence or user work. Cross-task schema changes require coordinator review. Agents update only
their task log; the coordinator periodically rolls status into the root plan.

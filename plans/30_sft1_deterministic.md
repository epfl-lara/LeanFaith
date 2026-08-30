# SFT1 — deterministic theorem-equivalence data at scale

> **Task ID:** SFT1
> **Status:** waiting_user
> **Owner/session:** unassigned
> **Last updated:** 2026-08-30
> **Approval recorded:** pending
> **Dependencies:** REPR `goal_v1.0`; explicit user approval of transform catalog before bulk generation
> **Next gate:** inventory/propose preserving and breaking operations, then review them with user
> **Compute class:** CPU/RAM and Lean CPU for family certification/sampled audits; no LLM/GPU
> **Lean budget:** no per-pair compilation; persistent Meta generation and stratified checks only
> **Local staging root:** `/storage/milikic/leanfaith/value_first/sft1_deterministic_v1/`
> **HF destination:** private `Lemmy00/leanfaith-sft1-deterministic-v1`

## Hard user gate

Do not start bulk generation yet. The task owner must first:

1. inspect every existing preserving/breaking transform and its evidence;
2. inspect `/localhome/milikic/lean_theorem_equivalence` as prior direction, not authority;
3. propose additional high-value transform families and composition rules;
4. explain applicability, automatic label basis, cancellation risks, expected coverage, and Lean
   cost to the user;
5. receive explicit approval of the catalog and record it here.

`TRANSFORM_CATALOG_V2.md` and prior v1/v2 registries are provisional inputs. Their existence does
not constitute approval for this new large release. Until approval, status remains `waiting_user`
and only audit/design/smoke code may change.

## Objective and scale

Create diverse theorem-statement pairs with labels supplied automatically by reviewed transform
polarity. Commit target: 500K distinct theorem roots × five preserving and five breaking sampled
compositions = about 5M pairs. Stretch: 1M roots/about 10M pairs after quality/throughput evidence.

Source roots should span `formalmathatepfl/compiler_data`, Mathlib, Physlib, and CSLib. Deduplicate
by statement/signature before sampling. Cap compiler-data roots at 20% in v1 because unregistered
source declarations require root elaboration; report its separate yield/throughput before changing
the cap. Sample by source/domain/signature structure so one library or theorem family cannot
dominate. Theorem/lemma declarations only; `def` support is a future extension.

## Label and representation contract

Each pair uses `goal_v1.0`:

```text
reference: string
candidate: string
label: bool
```

- A composition made only of approved preserving transforms receives `label=true`.
- A composition containing at least one approved breaking transform receives `label=false`.
- The engine records a protected breaking site/certificate. Later operations must be disjoint or
  proven unable to reverse, shadow, or cancel that break.
- Applicability is determined by typed/syntactic preconditions, not an LLM.
- No LLM and no per-pair Lean compilation is allowed to create or confirm labels.

The sidecar stores root/source ID, representation provenance, ordered transform chain and seeds,
family/evidence classes, applicability decisions, protected site, certificates/audits, and hashes.
Rich relation/explanation fields are optional and may not block a core row.

## Existing assets to assess

- Current transforms under `src/leanfaith/transforms/`, Meta engines under `LeanFaith/Meta/`, and
  `TRANSFORM_CATALOG_V2.md`.
- 17,181 mixed-supervision pairs:
  `/storage/milikic/leanfaith/experimental_mixed_supervision/firsthop_kimi_qwen1125_composition_f7b398af_v1`
- 4,031 depth-three pairs:
  `/storage/milikic/leanfaith/deterministic_v2/composition_third_hop_audits/frontier_084859ee_five_families_v2`
- The larger 27,327 unary pool, whose exact path/eligibility DATA-REUSE must identify. It remains
  gated until transform review.

Classify code/assets as reuse, adapt, legacy reference, or retire-after-proof. Do not silently fold
old intention-based labels into the new release.

## Scope and ownership

**Before approval:** transform inventory, tests, proposed catalog, source sampling design,
composition/cancellation rules, `goal_v1.0` integration design, and bounded family smokes.

**After approval:** root selection, deterministic generation, sampled verification, sharding,
manifests, reuse import, and private publication.

**Out of scope:** LLM labeling, compiling every statement/pair, training, definitions, or changing
historic gate semantics in place.

**Writable paths:** this brief; `src/leanfaith/sft1/`; `LeanFaith/Meta/SFT1/`;
`configs/transformations/sft1_value_first_v1/`; `tests/unit/sft1/` and named SFT1 live fixtures;
the staging root. Existing transforms/Meta engines, shared Lean/backend/project/dependency paths,
and frozen v1/v2 registries are read-only inputs; request coordinator changes rather than mutating
them.

## Lean-efficiency plan

Lean is the bottleneck. Prefer existing elaborated library environments and typed Meta transforms:
load each homogeneous project once, discover applicable sites, transform many roots, and render
both goals in the same persistent session. Cheap syntax/static checks precede Lean. Do not launch
`lake env lean` per pair. Cache root elaboration, sites, transformations, and renderings by source
and project/toolchain/checker hashes.

Compile only family certification cases and a stratified production audit. For checks that are
actually necessary, adapt the pooled/persistent approach in
`/localhome/milikic/rl_theorem_provers/src/data/solve_new_problems/evaluate_lean.py`. Coordinate the
machine-wide worker budget across concurrent sessions.

## Execution gates

### Catalog/user gate

Deliver a table per transform: polarity, example, preconditions, evidence, composition safety,
expected coverage/value, implementation status, Lean cost, known risks, source/family/template caps,
and keep/change/drop recommendation. Proposed defaults are: compiler-data roots ≤20%, no source
above 40%, no emitted family above 15%, and no exact ordered composition template above 2%. Stop
for explicit user
approval and replace `Approval recorded: pending` with the dated decision before scale.

### One-example and family smokes

After approval, run one root end-to-end for every family, then 100 diverse roots. Prove stable IDs,
automatic labels, protected-negative behavior, goal serialization, journaling, and deterministic
resume. Never discard failures silently.

### 10K pilot

Run 10K roots with proposed source/family caps. Report applicability, unique pairs/root, label and
family balance, duplicates, audit failures, cache hits, Lean requests, throughput, memory, and full
projection, including root-elaboration rows/s by source. Compile a stratified sample rather than all
pairs. Train a cheap lexical/length-only canary on a family-held-out split; balanced accuracy must
remain below 0.70 or the dataset is rebalanced/capped. Conduct a blinded semantic QA audit of at
least 100 examples per family×polarity stratum where available. This audit measures family error
and never replaces automatic transform labels or adds per-row LLM labeling.

### Scale

Generate 500K roots in resumable shards. Compile at least 0.1% of emitted pairs and at least 100 per
family/source stratum where available. Stop if unexpected invalidity exceeds 1% overall or shows a
systematic family defect, if the canary reaches 0.70, or if the blinded audit exposes an unaccepted
family error rate. Screen both sides against `data/benchmarks/golden_blocklist_v1.json` using exact
and existing signature-near-duplicate hashes; exclude hits from training and record them. Stretch
to 1M roots only after the committed release passes review.

## Acceptance criteria

- User approval is recorded before bulk generation.
- Five useful preserving and five useful breaking compositions per root are targeted without
  forcing inapplicable/duplicate variants.
- Labels are automatic from approved polarity and breaking transforms cannot be canceled later.
- Numeric source/family/template caps, family-heldout canary <0.70, semantic QA, and gold screens
  prevent cheap shortcuts without turning QA judgments into labels.
- `goal_v1.0` rows contain only valid theorem signatures; optional metadata stays in sidecars.
- Lean use is persistent/cached/sampled, with no per-pair compilation.
- Source/family balance, audits, manifests, hashes, resume, and Hub revision are reproducible.

## Session kickoff prompt

```text
Own only SFT1 in /localhome/milikic/LeanFaith. Read AGENTS.md, PLAN.md,
plans/00_shared_contracts.md, plans/30_sft1_deterministic.md, and TRANSFORM_CATALOG_V2.md. Update
this brief and claim exact paths. Your first deliverable is a complete preserving/breaking transform
audit and proposal for the user, including applicability, composition safety, value, and Lean cost.
Do not bulk-generate data until the user explicitly approves it here. Lean is the bottleneck: no
per-pair compilation or LLM labels; use typed/persistent Meta work, caching, and stratified audits.
Preserve frozen tracks and user work. End with the exact user decision needed.
```

## Coordinator requests

- Explicit user decision on the proposed transform catalog is required before generation.

## Progress log (append-only)

- 2026-08-30 — task brief created in `waiting_user`; no new transform approved or data generated.

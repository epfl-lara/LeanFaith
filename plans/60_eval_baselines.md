# EVAL — golden v2 splits, baselines, and result registry

> **Task ID:** EVAL
> **Status:** not_started
> **Owner/session:** unassigned
> **Last updated:** 2026-08-30
> **Dependencies:** canonical 5,111 rows; REPR only for the additive `goal_v1.0` view/publication
> **Next gate:** freeze and verify the exact 2,555 validation / 2,556 test assignment
> **Compute class:** CPU plus external/local models; cached Lean checks; larger LLM jobs may need GPU/API approval
> **Lean budget:** typecheck/DefEq each necessary pair once and share cached results across baselines
> **Local staging root:** `/storage/milikic/leanfaith/value_first/eval_v2/`
> **HF destinations:** private `Lemmy00/leanfaith-eval-v2` and `Lemmy00/leanfaith-eval-results-v2`

## Objective

Create one simple evaluation-only benchmark from all 5,111 canonical gold pairs, then run and store
the requested baselines on both splits now. Later experiments tune models and thresholds on
validation; the selected final model is evaluated on the unchanged test and compared with the
already stored test baselines. There is no training split and no ceremonial one-time opening rule.

## Golden v2 split contract

Input:

- `/storage/milikic/leanfaith/golden/canonical/golden_pairs_v1.jsonl`
- `data/benchmarks/golden_partition_v1.json` for prior IDs/provenance only

Produce exactly:

- `validation`: 2,555 rows
- `test`: 2,556 rows
- total: 5,111 rows

The split assignment and its hash operate only on immutable raw canonical IDs/groups/labels and do
not depend on REPR. Freeze them immediately. After REPR freezes, add the `goal_v1.0` fields as a
derived configuration pinned to the representation hash; do not change split membership.

Use a fixed seed and group-aware, label/source/provenance-stratified assignment so ancestry and
duplicates do not cross splits. Use deterministic group-level subset selection (for example dynamic
programming or seeded greedy plus swap repair) to hit exactly 2,555/2,556; stop rather than splitting
a group if an exact assignment cannot be proved. Preserve every canonical row and raw text.

The source inventory contains 2,501 `expert_human` rows and 2,610 `auto_typecheck_fail` rows; all 47
`label_conflict=true` rows are expert-labeled. Target 1,250/1,251 expert rows per split and 1,227
non-conflict expert headline rows per split, subject to the group constraint, then record exact
achieved counts. The primary semantic headline is `label_provenance == expert_human` and
`label_conflict == false` (2,454 rows total). Auto-typecheck rows form an `auxiliary_validity`
diagnostic, never semantic gold or training data. Also publish an all-rows diagnostic clearly
labeled as mixed-provenance.

This policy explicitly supersedes the old 819 `golden_train` + 821 dev + sealed 910 test workflow.
Do not rewrite historical reports/manifests; archive them as prior evidence. ConsistencyCheck is not
substituted for this Lean↔Lean gold benchmark; it only informs `goal_v1.0` representation and small
source studies.

`policies/evaluation_use_v2.yaml` records the owner's authorization to run Codex, Claude, and Lemex
baselines on these evaluation-only pairs. Do not send unrelated private source data with them.

## Baselines

Run on both validation and test as soon as each implementation is ready, with row-level predictions
and versioned manifests:

- majority-class and identity/string-equality;
- Lean typecheck/validity and DefEq where applicable;
- BEq and BEq+;
- GTED/ASSESS family metrics (confirm exact names/contracts from primary implementations/papers);
- Codex, Claude, and Lemex under one frozen consistency rubric;
- all predefined two/three-model majority/consensus voting combinations;
- any already-frozen LeanFaith checkpoints as clearly historical baselines.

CriticLean is out of scope for EVAL v2. Its required natural-language input makes it an
NL↔Lean/autoformalization baseline rather than a directly comparable Lean↔Lean consistency
baseline, and scoring only the gold subset with natural-language fields would create selective
coverage. Reconsider it in a separate future NL↔Lean evaluation track alongside SFT2B rather than
mixing its score into this benchmark.

Do not force a baseline onto incompatible rows. Record coverage, abstentions/errors, latency/cost,
model/prompt revision, threshold/calibration source, and per-row raw prediction. Never convert
unknown/error to semantic false. Review the ReForm paper and the user-referenced gold source
(`arXiv:2510.24592v1`) from primary materials when implementing, and document whether it adds rows
or only comparison context before changing the 5,111-row contract.
Every baseline config records the consumed representation (`raw_headless`, `goal_v1.0`, or another
explicit view) so pre-REPR raw baselines remain comparable and honest.

## Metrics and reporting

Freeze baseline metrics before comparing trained models: balanced accuracy, accuracy, precision,
recall, F1, AUROC, AUPRC, Brier score, ECE, coverage/abstention, and bootstrap confidence intervals.
Report label/source/domain/difficulty and representation-length slices where metadata supports them.
Threshold-free and thresholded metrics must state how thresholds were chosen. Test thresholds may
not be tuned on test labels.

The results repository stores immutable run configurations, row-level predictions, aggregate JSON,
human-readable reports, checksums, environment/model/prompt revisions, and costs. New runs append a
versioned configuration; they do not overwrite prior scores.

## Scope and ownership

**In scope:** split builder/verification, contamination guard, baseline adapters, cached Lean
features, LLM judge/vote runs, metrics, prediction/result registry, reports, and private publication.

**Out of scope:** training on gold, test-tuned thresholds, CriticLean or other NL↔Lean-only
baselines, deleting old sealed artifacts, silently changing baseline prompts/models, or calling
ConsistencyCheck the main Lean↔Lean benchmark.

**Writable paths:** this brief; `src/leanfaith/eval2/`; `configs/eval2/`; `prompts/eval2/`;
`tests/unit/eval2/`; `reports/eval/value_first_v2/`; the staging root. Existing `eval/`, v1 split/
judge/project configs, shared Lean/provider/dependency paths, and historical reports are read-only;
request coordinator changes.

## Lean-efficiency plan

Lean is the bottleneck. Deduplicate required typecheck/DefEq requests across baselines and splits,
then execute once per unique context through persistent homogeneous pools. Cache by pair/source,
Lean/project/toolchain/options, and method version. Cheap string/metadata baselines use no Lean.
Reuse cached compilation evidence from source datasets when its exact contract matches; otherwise
run a bounded measured job. Coordinate the machine-wide worker budget and never spawn per-row Lean.

## Execution gates

### One-example split smoke and freeze

Trace one old train, dev, test, auto-typecheck, and conflict row into v2. Verify total/unique IDs,
group-disjointness, exact split counts, label/source/provenance balance, deterministic rebuild hash,
no training export, and unchanged raw canonical text. Independently recompute the assignment once.
When REPR is available, separately verify the derived `goal_v1.0` view/provenance without touching
the frozen split/hash.

### Baseline smoke

Run one positive and one negative through the baseline adapter/output/metric stack, including an
abstention/error. For Lean/LLM baselines, prove caching/resume avoids duplicate calls.

### Full baseline registry

Run all feasible baselines on both splits, store predictions immediately, and publish versioned
results. Missing/unavailable baselines remain explicit tasks with reasons; one missing method does
not block freezing the dataset or other scores. Request compute/API resources with a measured pilot.
The historical `leanfaith-eval` v1 command enforces the retired final-test seal; implement the
additive `eval2` path and do not use `--unseal-final-test` to mutate/relabel v1 artifacts.

## Acceptance criteria

- Exact deterministic 2,555/2,556 split covers all 5,111 unique canonical IDs and has no cross-split
  group leakage or training view.
- Primary expert-human/non-conflict, auxiliary-validity, and all-row diagnostic populations are
  explicit and preserve `label_provenance`.
- Every score is reproducible from row predictions and a pinned configuration.
- Validation and test baselines are available without implying later model tuning may use test.
- Errors/abstentions, coverage, cost, and incompatible-row handling are honest.
- Lean/LLM calls are cached/resumable and both Hub repositories match local hashes/counts.

## Session kickoff prompt

```text
Own only EVAL in /localhome/milikic/LeanFaith. Read AGENTS.md, PLAN.md,
plans/00_shared_contracts.md, and plans/60_eval_baselines.md completely. Update this brief and claim
exact paths. First build and independently verify an evaluation-only 2,555 validation / 2,556 test
split from raw IDs/groups for all 5,111 canonical pairs; it does not wait for REPR and never exports
gold for training. Pin any later goal_v1.0 view to the same split. The old sealed workflow is
historical. Then run/store baselines on both splits with pinned predictions and honest coverage.
Lean is the bottleneck: deduplicate and cache shared typecheck/DefEq work in persistent pools. Do
not tune thresholds on test. Keep ConsistencyCheck as representation inspiration, not the canonical
Lean↔Lean benchmark. Record every revision, score artifact, blocker, and next action.
```

## Coordinator requests

- Approve any proposed additions to the 5,111-row benchmark before changing the frozen v2 contract.
- Provide larger GPU/API placement for model baselines after a measured pilot request.

## Progress log (append-only)

- 2026-08-30 — task brief created; no split or baseline run performed.
- 2026-08-30 — owner decision: CriticLean moved out of EVAL v2 because its natural-language input
  is not directly comparable to the benchmark's Lean↔Lean contract; defer it to a future NL↔Lean
  evaluation track.

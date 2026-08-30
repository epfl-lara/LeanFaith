# Claude Fable 5 maximum-reasoning setup review

> **Review date:** 2026-08-30
> **Reviewer:** `claude-fable-5`
> **Effort:** `max`
> **Mode:** read-only (`plan` permission mode; no edits, network jobs, Lean, models, or pipelines)
> **Initial verdict:** NOT READY
> **Final re-review:** pending after coordinator fixes and setup commit

## Scope

Claude read the full setup diff, every active coordinator/task document, source policies,
provisional transform catalog, validator and tests, the prior archived ledger, relevant source
configs, the canonical 5,111-row gold file, and host-resource evidence. It independently ran the
network-free plan validator. The review explicitly tested session self-sufficiency, ownership
collisions, Lean leakage, label/schema rules, source authorization, evaluation semantics,
resumability, and validator strength.

## Initial findings and disposition

| # | Severity | Finding | Disposition and resolution |
| ---: | --- | --- | --- |
| 1 | P0 | The setup was uncommitted, so new worktrees would see the retired plan and no briefs. | Accepted. The coordinator setup is committed as one baseline after all fixes; the hash is recorded in `plans/README.md`. The unrelated untracked `from_mixed_v0.py` is excluded. |
| 2 | P0 | `goal_v1` had no owner/implementation path; SFT/EVAL could create incompatible renderers, and no compile-context/inverse rule existed. | Accepted. Added the REPR task, frozen `goal_v1.0`, tagged elaborated/surface modes, raw `compile_context`, and a rule that compilation uses raw generated source rather than reconstructing from goal text. Downstream briefs depend on REPR. |
| 3 | P1 | Plain `uv sync` can prune Torch/Transformers from the shared environment. | Accepted. README/AGENTS require `uv sync --group dev --group local-inference` and warn against the plain command in shared checkouts. |
| 4 | P1 | The proposed semantic headline would mix 2,501 expert labels with 2,610 auto-typecheck-failure labels. | Accepted. All 5,111 rows remain in the exact half/half dataset, but the 2,454 expert-human/non-conflict rows define the semantic headline; auto rows are an auxiliary-validity diagnostic. Provenance is stratified and retained. |
| 5 | P1 | No gold-contamination rule covered new CPT/SFT releases. | Accepted. Shared contracts require the existing blocklist; SFT checks exact/near-duplicate signatures, CPT records cheap exact hits, and each task documents exclusion/reporting. |
| 6 | P1 | Concurrent briefs claimed overlapping `train2`, `generation`, `collect2`, `eval`, configs, and tests. | Accepted. Every task now owns a disjoint package/config/prompt/test directory. Shared backend/provider/dependency/project surfaces are coordinator-owned and changed only through a coordinator request. |
| 7 | P1 | There was no numeric machine-wide Lean/RAM/GPU reservation. | Accepted. Default is one worker per task, at most two concurrent Lean workers and 40 GiB combined measured Lean RSS, plus one local GPU job. The coordinator owns a reservation table. |
| 8 | P1 | Historical v1 fail-closed source configs contradicted the new owner authorization. | Accepted. Added active v2 source/evaluation policies, a policy authority index, historical banner on v1, and explicit v2 authorization/additive-config rules in SFT2/EVAL briefs. V1 configs remain immutable. |
| 9 | P1 | SFT1 gates measured validity but not shortcut leakage or semantic transform error. | Accepted with a bounded audit. Added numeric source/family/template caps, a family-heldout lexical/length canary below 0.70, and a blinded per-family/polarity QA sample. QA never replaces automatic labels or becomes per-row labeling. |
| 10 | P1 | CPT2 row-level validation would leak repeated theorem prefixes across train/validation. | Accepted. Split by exact theorem-string hash, select whole groups, balance as group constraints allow, and report prefix/body length quantiles. |
| 11 | P2 | `TASK_TEMPLATE.md` did not satisfy its own validator, and hardcoded task names let new numeric briefs escape checks. | Accepted. Template metadata/sections now match; the validator discovers all numeric task briefs while enforcing the required main set. |
| 12 | P2 | Validator checked headings but not dangerous task invariants; later quoted metadata could override headers. | Accepted. It parses only the first metadata block, checks owners for active states, SFT1 approval before scale, private destinations, unique staging roots, and frozen task-specific anchors. Tests cover metadata override. |
| 13 | P2 | SFT2A “double agreement” was proposer intent plus one independent semantic judge. | Accepted. Manifests call it `proposer_intent+single_judge`; a blinded 10% Lemex audit detects systematic problems. Legacy rows with two semantic judgments remain separately identified. |
| 14 | P2 | Active docs dropped hard-won operational gotchas. | Accepted. AGENTS records closed stdin for Codex/Lemex, safe environment sync, Lean memory-limit caveat, and the additive EVAL v2 path rather than bypassing the historic seal. |
| 15 | P2 | Several v1 policies still appeared authoritative for the retired regime. | Accepted. `policies/README.md` defines active value-first authority and treats v1 policies as immutable replay rules, not active-task defaults. |
| 16 | P2 | SFT1 compiler-data roots could require expensive per-root elaboration at huge scale. | Accepted. Roots are statement-deduplicated, compiler data is capped at 20%, and the 10K pilot reports root-elaboration yield/throughput separately. |
| 17 | P2 | DATA-REUSE omitted known exact roots and the curated 469,585-row CPT artifact. | Accepted. Added the exact provisional unary path/leak warning and curated CPT inventory; CPT1 omission is explicitly intentional under the user's selected v1 inputs. |
| 18 | P2 | Small drift: five checks called four, schemas differed, Codex-specific edit wording, exact group-count feasibility, and shared pre-commit risk. | Accepted. README count and schema wording are fixed; edit wording is agent-neutral; EVAL requires deterministic group-level exact-count solving and fails closed rather than splitting groups; separate worktrees are preferred. |

## Coordinator decisions resolving reviewer questions

1. **Representation source:** use elaborated rendering whenever the environment is already loaded or
   candidate compilation is already required; otherwise use a deterministic surface fallback.
   Persist `goal_v1_source` and report slices. This follows the user's value/cost rule and avoids a
   new mass-compilation prerequisite.
2. **Evaluation headline:** preserve all 5,111 rows in validation/test as requested. Use only
   expert-human/non-conflict rows for the primary semantic-consistency headline; keep the full set
   and auto-typecheck-failure subset as explicit diagnostics.

## Strong parts the reviewer said not to regress

- Short coordinator plan plus self-contained task briefs, kickoff prompts, closed statuses, and
  append-only task logs.
- Concrete 1/100/10K Lean gates, zero-Lean CPT1/TRAIN, and the 500-row CPT2 oracle rather than a
  4.36M-row compile.
- Correct label separation: invalid is not semantic false, unknown remains auditable, CPT2 retains
  `isValid`, and gold is never overwritten.
- Hard SFT1 user gate; SFT2A sibling retention/three-attempt cap; SFT2B three-judge majority.
- Private-first `Lemmy00` destinations, pinned manifests/Hub revisions, and secret hygiene.
- Historical archive preservation, provisional transform-catalog banner, and protection of the
  unrelated untracked `from_mixed_v0.py`.
- Both evaluation splits may receive baselines now, while model/threshold tuning remains
  validation-only.

## Final re-review

Pending a clean committed coordinator baseline. The final Fable verdict and any remaining findings
will be appended here without rewriting the initial review.

# LeanFaith value-first parallel data plan

> **Status:** approved; handoff setup ready for independent task sessions
> **Approved:** 2026-08-30
> **Current scope:** prepare, validate, and publish the datasets and evaluation assets. Do not start
> full training from this coordinator task.

LeanFaith is building a learned judge of whether two Lean theorem statements express the same
mathematical claim. The immediate goal is to create high-value data quickly: very large, cheap
automatic supervision first, followed by smaller and richer LLM-assisted supervision. CPT and SFT
stages remain ablations; evaluation is mandatory.

The previous execution ledger is preserved at
[`docs/archive/PLAN-2026-08-30-refocus-v3.md`](docs/archive/PLAN-2026-08-30-refocus-v3.md).
It is evidence and reusable history, not the active task list.

## Non-negotiable operating rule

Lean is the bottleneck. Every task must do text, schema, provenance, filtering, and deduplication
without Lean when possible. When Lean is necessary, use a persistent environment, batch requests,
bounded parallel workers, content-addressed caches, and staged samples. Never launch one Lean
process per row or recompile an entire corpus by default. The full contract is in
[`plans/00_shared_contracts.md`](plans/00_shared_contracts.md).

## Parallel workstreams

| Workstream | Task brief | Initial state | May start independently? |
| --- | --- | --- | --- |
| Shared goal representation | [`plans/02_goal_v1.md`](plans/02_goal_v1.md) | not started | yes; enables SFT/eval text |
| Existing-data reuse | [`plans/05_existing_data_reuse.md`](plans/05_existing_data_reuse.md) | not started | yes |
| CPT phase 1 | [`plans/10_cpt1.md`](plans/10_cpt1.md) | not started | yes |
| CPT phase 2 | [`plans/20_cpt2.md`](plans/20_cpt2.md) | not started | yes |
| SFT phase 1 deterministic | [`plans/30_sft1_deterministic.md`](plans/30_sft1_deterministic.md) | waiting for transform review | inventory only |
| SFT phase 2A LLM transforms | [`plans/40_sft2_llm_transforms.md`](plans/40_sft2_llm_transforms.md) | not started | yes, pilot only |
| SFT phase 2B autoformalization | [`plans/50_sft2_autoformalizer.md`](plans/50_sft2_autoformalizer.md) | not started | yes, source/pilot work |
| Evaluation and baselines | [`plans/60_eval_baselines.md`](plans/60_eval_baselines.md) | not started | yes; mandatory |
| Training and ablations | [`plans/70_training_ablations.md`](plans/70_training_ablations.md) | deferred | design/smokes only |

These streams are intentionally parallel. REPR freezes the shared `goal_v1.0` serializer while
CPT1, CPT2, DATA-REUSE, and the EVAL split work proceed. SFT/evaluation serialization depends on
REPR, but their source/prompt/baseline preparation can start. Existing-data reuse informs SFT1/SFT2
but does not need to finish before CPT or evaluation. SFT1 bulk generation has one hard user gate:
its owner must first review the complete preserving/breaking transform catalog with the user. Full
training waits for versioned datasets and frozen evaluation inputs, but architecture preparation
may proceed.

## Shared decisions

- Model-facing theorem pairs use the compact `goal_v1.0` representation: ordered locals and
  hypotheses followed by exactly one `⊢ target`. The declaration name, shell, imports, and proof
  are removed. CPT2 is the intentional exception and stores the statement/context prefix plus
  proof body.
- Minimal release schemas are `{text}` for CPT1, `{theorem, body, label}` for CPT2,
  `{reference, candidate, label}` for SFT, and
  `{pair_id, reference, candidate, label, split}` for evaluation. Rich metadata belongs in a
  sidecar and may enrich SFT2, but it must never block cheap SFT1 generation.
- The main equivalence datasets contain both valid equivalent and valid non-equivalent pairs.
  Syntactically invalid candidates are useful, but live in a separate validity/acceptability view.
- New dataset repositories are private-first under `Lemmy00`. Every release pins inputs and
  records code revision, schema version, counts, hashes, seeds, and provenance.
- All 5,111 canonical gold examples become evaluation-only: deterministic 2,555 validation and
  2,556 test, with no training split. The expert-human, non-conflict subset is the primary semantic
  headline; auto-typecheck labels are an auxiliary validity diagnostic. Baselines may be run and
  stored on both now; model selection uses validation and the final selected model is compared on
  the unchanged test set.
- Initial model family: Ettin-150M first, ModernBERT as the control. Encode the two sides
  separately, cross-match them with decoder-style cross-attention layers, then predict a binary
  probability. Other architecture families are deferred.
- Theorem statements are in scope now. Applying the same system to `def` declarations is a future
  extension.

## Session and ownership model

The coordinator owns this file, [`plans/README.md`](plans/README.md), and cross-task decisions.
Each task session owns only its task brief and the code/config/test paths it explicitly claims in
that brief. A worker must not silently change another task's contract or root status. Shared-code
changes require a short compatibility note in both affected task logs.

Start a session by copying the kickoff prompt from the relevant task brief. Before implementation,
the session must record its owner, status, exact writable paths, first one-example smoke, and Lean
budget. Progress is recorded in that task brief, not by rewriting this coordinator plan.

## Delivery order

1. Complete one-example end-to-end smokes and freeze schemas/manifests.
2. Run bounded pilots with measured throughput and quality gates.
3. Ask the user for compute or policy decisions where the task brief says to stop.
4. Scale only after the pilot passes; keep every long job resumable and run unattended work in a
   verified named `tmux` session that survives the launching agent turn.
5. Publish data and manifests to the named private Hugging Face repository.
6. Run evaluation baselines on validation and test; later select models on validation only.

The setup index, status protocol, communication templates, and repository map are in
[`plans/README.md`](plans/README.md).

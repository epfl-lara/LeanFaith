# Git-only production review packet

Date: 2026-09-05. Owner: Codex coordinator `/root`.
Review branch: `milikic/sft1-production-review-20260905`.
Parent implementation: `7498fbf4e78421012c1c729d05576c345c5f16f1`.
Repository: <https://github.com/epfl-lara/LeanFaith>.

## Purpose

Review the shortest sound path to substantial SFT1 generation, then training. The user wants
concrete data delivery and fewer repeated failed gates. Assess the diagnosis independently;
the proposed fixes have not been implemented. Recommend precise policy/code changes, not another
general architecture exercise. A newly discovered correctness blocker is in scope even if it
changes the proposed plan.

This packet and its parent implementation are in Git. No `/storage` directory, local worktree,
private Hub access, running job, chat history, or live Lean environment is needed to read it.
Original absolute paths in captured receipts are provenance only. Do not follow them as inputs.

## Read order

1. Root [`PLAN.md`](../../../PLAN.md) and [`plans/00_shared_contracts.md`](../../00_shared_contracts.md).
2. The active beginning of [`plans/30_sft1_deterministic.md`](../../30_sft1_deterministic.md),
   especially Wave 3–5 execution, standing authorization, current next action, and the last progress
   entries. Earlier frozen policies embedded later in that long file are historical evidence.
3. [`production_review.md`](production_review.md) for the complete state summary and proposed order.
4. The compact [`wave4/expected_report.json`](wave4/expected_report.json) and
   [`replay_wave4.py`](replay_wave4.py), then the implementation entrypoints below.
5. [`evidence/release_metadata.json`](evidence/release_metadata.json), the four terminal/summary
   snapshots, and the compiler preflight rejection only where they support a specific finding.

The historical root workstream table is not a current production ledger. The SFT1 brief on this
branch is newer. SFT2A/B production tips are separately pinned below; this branch does not pretend
to integrate those independent implementations.

## Claims to verify or challenge

| Claim | Evidence available in Git | Review question |
| --- | --- | --- |
| Latest SFT1 core is 13,984 pairs and narrow; 508,600 N19 pairs are auxiliary | `evidence/release_metadata.json`, including recorded 0.963 relation-parity diagnostic | What sampling/coverage must precede a useful first training comparison? |
| Merging completed Wave 4 runs rejects six duplicate rendered-pair classes | `wave4/` projection and actual `materialize_wave4_records` replay | Is deterministic removal of competing whole groups sufficient? |
| Whole-group deduplication retains 202 roots/1,161 rows, then existing balance keeps 15 roots/60 rows | Same replay using actual production selection code | Is the physical-row balancing contract structurally incompatible with shared-base composition? |
| Descriptor enumeration builds an orbit before applying the five-variant selection | `Sprint.lean`, `square.py`, preserved failure marker, active task log | Where should deterministic work/memory bounds apply, including re-enumeration for certification? |
| One source-context rejection prevents the strict compiler audit from passing | `wave5_preflight_rejection.json`, `compiler_certificate_gate.py` | How should source coverage be separated from 100% evidence on retained pairs? |

For one base with `n` variants, the physical graph has `2n` positive rows and `n+1` negative rows.
Its signed class surplus is `n-1`. Examine whether this is sufficient to prove the identified
inverse-vector balancing problem, and what limitations that argument has across multiple groups.
Do not mistake a successful certificate for evidence of a challenging training distribution.
The replay also distinguishes 69 positive-surplus roots, 133 roots with equal total labels, and
only 13 exactly zero cell-vector roots. Exact inverse matching retains two additional roots.
Shared-base imbalance is not the entire explanation for the severe selection loss.

## Code map on this branch

All paths below are repository-relative and their code is unchanged by this review branch.

- [`src/leanfaith/sft1/sprint/square.py`](../../../src/leanfaith/sft1/sprint/square.py):
  `preselect_wave4_variant_descriptors`, `materialize_wave4_records`,
  `_rematerialize_wave4_selection`, `_balance_wave4_pair_delta_units`,
  `select_wave4_release_groups`, `build_wave4_release`.
- [`LeanFaith/Meta/SFT1/Sprint.lean`](../../../LeanFaith/Meta/SFT1/Sprint.lean):
  `buildWave4Descriptors`, `buildWave4Orbits`, `rebuildSelectedWave4Orbits`,
  `compilerWave4DescriptorPayload`, `rebuildSelectedCompilerWave4Orbits`.
- [`src/leanfaith/sft1/sprint/compiler_certificate_gate.py`](../../../src/leanfaith/sft1/sprint/compiler_certificate_gate.py):
  root execution/terminal classification and the checks including
  `all_sample_roots_terminal`, `all_source_proofs_checked`, `all_wave3_certificates_exact`,
  and `all_wave4_closures_exact`.
- [`src/leanfaith/sft1/sprint/compiler_replay.py`](../../../src/leanfaith/sft1/sprint/compiler_replay.py)
  and [`compiler_scale.py`](../../../src/leanfaith/sft1/sprint/compiler_scale.py):
  source reconstruction, bounded audit, reusable terminal identities, production integration.
- [`src/leanfaith/sft1/sprint/integrity.py`](../../../src/leanfaith/sft1/sprint/integrity.py)
  and [`views.py`](../../../src/leanfaith/sft1/sprint/views.py): shared-edge integrity and shortcuts.
- [`configs/transformations/sft1_value_first_v1/wave4_v1.yaml`](../../../configs/transformations/sft1_value_first_v1/wave4_v1.yaml)
  plus the Wave 5 config in that directory: active caps and configured operations.
- Relevant existing tests: `tests/unit/sft1/test_wave4_lean_free.py`,
  `test_wave4_provenance.py`, and the `test_wave5*` files in the same directory.

## Reproduction and evidence limits

The packet's Wave 4 replay uses only Git-contained projected records and the current Python
materializer/selection functions. It starts no Lean worker and calls no model or network service.
With this repository's Python dependencies available, run from the repository root:

```bash
PYTHONPATH=src python plans/reviews/2026-09-05-production/replay_wave4.py
```

The projection supports duplicate, group-closure, and balancing analysis. It is not a replacement
for original proof certificates, a kernel replay, a final release, or the pending manual inspection.
See the projection manifest and replay output for exact source identities and omitted fields.
If execution is unavailable, inspect the expected report and production functions directly.

`evidence/release_metadata.json` contains selected fields with original file hashes. Full corpus
files are deliberately absent, so independently recounting all CPT/SFT releases is outside this
packet. The recorded counts are audit evidence, not newly reproduced corpus totals.
The compiler failure marker establishes a failed run; its detailed memory diagnosis is recorded in
the committed SFT1 log and should be assessed against the enumerator code. We do not claim this
packet itself replays an out-of-memory incident.

## Other workstreams: bounded context

- SFT2A production tip: `519c9d1800ed6527c5c3967278cf3e50849b5846`, branch
  `milikic/sft2a-72h-sprint`. Its two compacted shard manifests and shard-3 terminal failure are
  exported in `evidence/release_metadata.json`. Inspect its pinned
  [root ownership implementation](https://github.com/epfl-lara/LeanFaith/blob/519c9d1800ed6527c5c3967278cf3e50849b5846/src/leanfaith/sft2a/parallel_rehearsal.py)
  only if reviewing the proposed bounded recovery task. The 1,929 completed-root rows in shard 3
  are a reported local audit count; their full corpus/journal is not included here.
- SFT2B production tip: `870425e3bd5a37cf08ddbae820cb1d35537c7d32`, branch
  `milikic/sft2b-scale-sprint-v3`. Inspect the pinned
  [scale launcher](https://github.com/epfl-lara/LeanFaith/blob/870425e3bd5a37cf08ddbae820cb1d35537c7d32/src/leanfaith/sft2b/scale_sprint_v3.py),
  especially `queue_downstream`. Its 100-row judge-pilot count is captured metadata; the full
  candidate/model-output corpus is not included. GPU activation and the downstream consumer must
  not become prerequisites for SFT1.
- EVAL: [`evidence/eval_baseline_status.md`](evidence/eval_baseline_status.md) and the publication
  receipt in the metadata file preserve the split/baseline status without touching EVAL-owned code.
  Its old REPR-wait language is stale; reconcile against the frozen representation in this branch.

## Required review result

Give a clear decision: proceed with the focused correction, revise it, or stop for a concrete
correctness issue. Distinguish code-proven findings, replayed findings, recorded observations, and
unresolved hypotheses. For each critical change, identify exact files/functions, the proposed
acceptance rule, the smallest meaningful regression, and which completed artifacts remain reusable.

Specify a practical contract for generated certified pairs versus training views, duplicate/group
handling, class and shortcut sampling, bounded enumeration, source eligibility, and cache reuse.
Then provide an ordered next-24–48-hours execution plan leading to incremental publication,
followed by a measurable first-training checkpoint. Treat time estimates as conditional on
measured throughput, and separate required user/compute decisions from already-authorized work.

Lean is the bottleneck: cheap parsing/filtering/selection first, persistent workers and compatible
caches for necessary checks, no whole-corpus recompilation. Preserve exact label evidence,
gold exclusion, stable IDs, complete groups, deterministic resume, and frozen artifacts. Avoid
inventing extra approval layers or turning optional mechanisms into prerequisites for useful data.

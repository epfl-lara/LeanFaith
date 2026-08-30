# DATA-REUSE — existing data inventory and direct-reuse map

> **Task ID:** DATA-REUSE
> **Status:** not_started
> **Owner/session:** unassigned
> **Last updated:** 2026-08-30
> **Dependencies:** none
> **Next gate:** produce a row-count/hash/schema inventory without generating or compiling data
> **Compute class:** CPU and storage only
> **Lean budget:** zero by default; reuse stored verifier evidence
> **Local staging root:** `/storage/milikic/leanfaith/value_first/reuse_inventory_v1/`
> **HF destination:** none; downstream task owners publish selected rows in their own repositories

## Objective

Prevent expensive regeneration and accidental mixing by classifying all promising existing
LeanFaith artifacts as direct reuse, revalidation needed, legacy/smoke reference, or reject. This
task creates an inventory and import recipes; it does not create new supervision or relabel rows.

## Scope and ownership

**In scope:** inspect manifests, schemas, row counts, hashes, label provenance, compilation evidence,
source restrictions, representation compatibility, duplicates, and intended destination task.

**Out of scope:** new LLM calls, bulk Lean checks, merging into final datasets, changing labels,
deleting old artifacts, or publishing rows.

**Writable paths:** this task brief; `src/leanfaith/data_reuse/`; `configs/data_reuse/`;
`tests/unit/data_reuse/`; the staging root above. Existing source artifacts and shared packages are
read-only. Preserve untracked user work.

## Required inventory

1. **SFT1 bootstrap — 17,181 pairs**
   `/storage/milikic/leanfaith/experimental_mixed_supervision/firsthop_kimi_qwen1125_composition_f7b398af_v1`
   The checked-in `src/leanfaith/corpus2/from_mixed_v0.py` is the reproducibility adapter that
   projected this legacy source into the frozen corpus-v0 trainer files. It is not the builder for
   the new value-first SFT1 release.
2. **SFT1 depth-three — 4,031 pairs**
   `/storage/milikic/leanfaith/deterministic_v2/composition_third_hop_audits/frontier_084859ee_five_families_v2`
3. **SFT1 provisional unary pool — 27,327 pairs** at
   `/storage/milikic/leanfaith/deterministic_scale/run_76de447_public_schema4_v1/unary/provisional_merged`.
   Prior evidence identifies 15,205 P01 rows with an `lf_alpha` lexical leak and 12,122 other rows;
   verify rather than blindly reuse. Do not approve/import it before the SFT1 transform review.
4. **SFT2A legacy tranche — 13,373 compiled Qwen/Kimi pairs with Codex judgments**
   `/storage/milikic/leanfaith/corpus2/recovered_singlepass_codex_v1_e8567ba/outputs/trainer_records.jsonl`
   Expected audit facts: 13,367 resolved, 6 unresolved, 307 positive, 13,060 negative.
5. **SFT2B pilot — 301 Mathlib docstring/generated-Lean records** under
   `data/raw/real_outputs/gate3_docstrings_operational_v1/`, including `unresolved_nl_lean.json`
   and `unresolved_pairs.jsonl` across the corresponding domain directories.
6. **Legacy mixed-corpus smoke — 23,414-row corpus v1**
   `/storage/milikic/leanfaith/corpus2/v1_ed41471/`. Use for schema/smoke reference only; it mixes
   supervision sources and is not the canonical new release.
7. **Evaluation-only gold — 5,111 pairs**
   `/storage/milikic/leanfaith/golden/canonical/golden_pairs_v1.jsonl` plus
   `data/benchmarks/golden_partition_v1.json`. Never route these rows to training.
8. **Curated Lean CPT corpus — 469,585 rows** at
   `/storage/milikic/lean_cpt_updates/2026-08-12-curated-libraries/hf_cpt_dataset.jsonl`. Inventory
   it as a possible later CPT ablation. Its omission from CPT1 v1 is intentional because the user
   selected only `lean-docs` and feedback training for the first release.

Search current manifests/reports for additional verified assets, especially CPT corpora, goal
representations, source catalogs, compilation caches, and LLM journals. Add them with evidence; do
not assume a path is reusable because it contains `valid=true`.

## Output contract

Create a checksummed manifest and human-readable report with one record per artifact:

```text
artifact_id, path, immutable_hash, rows, schema, source_lineage, label_source,
lean_evidence, representation, redistribution, destination_task,
decision (direct_reuse|revalidate|legacy_reference|reject), reason
```

For row-level importable assets, add a deterministic adapter preview containing stable IDs and the
target minimal schema. Do not copy all rows until the destination task accepts the recipe.

## Lean-efficiency plan

Lean is the bottleneck. This audit uses zero Lean by default: read durable manifests, journals,
cached verdicts, hashes, and samples. If evidence is missing, mark `revalidate` and propose the
smallest stratified check; do not launch it from this task. Never reinterpret process exit or a
`valid` field as stronger evidence than its recorded checker contract.

## Execution gates

### One-example smoke

Trace one candidate from an immutable source record through its manifest, verifier/judge evidence,
stable ID, proposed minimal row, and sidecar. Prove the source is not modified.

### Pilot

Inventory all eight required roots, reconcile expected versus observed counts, sample at least five
rows per schema/label source, and detect duplicate/ancestry overlap between the two SFT1 roots and
the SFT2A legacy root. Stop on missing or mutable provenance rather than guessing.

### Completion

Emit the inventory, adapter previews, count/hash reconciliation, explicit destination decisions,
and coordinator requests. Destination owners independently decide whether to import.

## Acceptance criteria

- Every required root is found or has a documented blocker.
- Counts, schema, label source, representation, and verifier evidence are evidence-backed.
- Gold rows are marked evaluation-only; invalid and unknown rows are not silently converted to
  semantic negatives.
- No Lean, LLM, upload, destructive cleanup, or source mutation occurred.
- SFT1 provisional transforms remain gated on user approval.

## Session kickoff prompt

```text
Own only DATA-REUSE in /localhome/milikic/LeanFaith. Read AGENTS.md, PLAN.md,
plans/00_shared_contracts.md, and plans/05_existing_data_reuse.md completely. Update this brief's
owner/status and claim exact writable paths. Perform a read-only, hash-backed inventory first.
Lean is the bottleneck: use existing durable evidence and do not compile anything by default. Do
not generate, relabel, merge, upload, or delete data. Produce adapter previews and decisions for
downstream owners, then record evidence and the exact next action in this file.
```

## Coordinator requests

- None yet.

## Progress log (append-only)

- 2026-08-30 — task brief created; no inventory execution performed.

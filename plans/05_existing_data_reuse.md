# DATA-REUSE — existing data inventory and direct-reuse map

> **Task ID:** DATA-REUSE
> **Status:** complete
> **Owner/session:** Codex `/root` — 2026-08-30 DATA-REUSE session
> **Last updated:** 2026-08-30
> **Dependencies:** none
> **Next gate:** SFT2A owner accepts or rejects the recorded legacy adapter recipe; SFT1 remains user-gated
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

**Paths claimed by this session:** `plans/05_existing_data_reuse.md`;
`src/leanfaith/data_reuse/`; `configs/data_reuse/`; `tests/unit/data_reuse/`;
`/storage/milikic/leanfaith/value_first/reuse_inventory_v1/`. The inventory pass begins read-only;
no source artifact, shared package, or other task path is claimed.

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

- **Exact next action:** the SFT2A owner reviews and accepts or rejects
  `sft2a_qwen_kimi_codex_legacy_v1`, freezes its `goal_v1.0` adapter, chooses the policy for seven
  duplicate directed text-pair rows, keeps six unknown judgments sidecar-only, and asks DATA-REUSE
  to rerun the preview before any import.
- The SFT1 owner must keep bootstrap, depth-three, and the 12,122 non-P01 unary rows gated until the
  user approves the transform catalog. Reject the 15,205-row P01 tranche as leaked; do not request
  DATA-REUSE to replay or compile it.
- The EVAL v2 owner may reuse all 5,111 canonical gold rows and labels for evaluation only, but must
  assign the active 2,555/2,556 split rather than reuse the legacy partition routing.
- A future CPT owner must assign content-derived IDs, rerun the gold screen, and complete source
  license review before evaluating the 469,585-row curated corpus as an ablation.

## Completed evidence and downstream decisions

- The checksummed inventory contains 22 artifacts and 117 deterministic adapter previews at
  `/storage/milikic/leanfaith/value_first/reuse_inventory_v1/`. Its external SHA-256 values are:
  `inventory_v1.jsonl=7c387ecfdd66a3365cde00ad8f46b9dc01be49d68d74db3ce7a605317033f52f`,
  `adapter_previews_v1.jsonl=9b1b4c2706d815c3d0efd1c3dc884500ae7a4dec31de52c407659e7a88e12283`,
  `evidence_v1.json=4e22b1d27b042418eb2809c8c0386097129d90a6832de811468fe4adba1b8d87`,
  `report_v1.md=4271af51bacaaddd5ca69666752d455e6d636a92462412c4a0edcee520c93be9`,
  and `manifest.json=40d0d270d3e0c9f3f1bccf01cbb5aa7b2a0d60bf19b12c733701f1e8afd1e200`.
  A second full run reproduced all five hashes exactly, so rerunning replaces the same bounded
  outputs rather than accumulating duplicates.
- The final manifest binds the formatted inventory implementation at
  `src/leanfaith/data_reuse/inventory.py` by SHA-256
  `edb1d63461a47051720f8a4e3ba512e369e78739abb21c511cba4552be2044e9`, in addition to the
  existing config and output hashes. The four scientific output hashes remained byte-identical
  after formatting, typing, and manifest-binding changes.
- The eight required inputs reconciled exactly: bootstrap 17,181 at tree hash
  `763c97ad109319e7b8414bae929bc59436b76a9a55fe5b994f74311b95876488`; depth-three
  4,031 at `8b5888ca556fa4a79ed0233c64a809b1bf0b2296a9e7a153e6ceeb825ee6d313`;
  unary 27,327 at `e8cf64154f202effc3e0943b81f7664d9bc02eb2186c8111314c1f696201d63c`;
  SFT2A 13,373 gross at `d49bad5cbe0f8a19ff76e285d958503d4c96d80afaf2571497ee1988ad970622`;
  canonical SFT2B 301 at `0e53c7934fa816e09ef47516b0505fba19e7159441218770595d3a1c1383aa41`;
  legacy mixed 23,414 at `4b762c120febe894e2e966e241674eb7ba60c35f4eb1ca1d8b1b9cd7519a65c3`;
  gold 5,111 at file hash `5f26c9b1b126e8bc9fe714f3c17fe68ad1d9b3aac60b19d80fdb4993ac8ed4e1`;
  and curated CPT 469,585 at tree hash
  `1757f4cb915f97484a4fc3be357ccd0aa1db2e4da9d45b99fa4179963bcf1651`.
- SFT1 bootstrap and depth-three are `revalidate`, not labels ready for direct training. Unary P01
  is rejected after an exact 15,205-row theorem-ID set match to candidate views containing
  `lf_alpha`; the other 12,122 unary rows remain `revalidate` because the merged root records
  `merge_replayed_with_lean=false`. Exact directed-pair overlap is 259 between bootstrap/depth-three,
  1,324 between bootstrap/SFT2A, and zero between depth-three/SFT2A; ancestry overlap is 1,523,
  2,429, and 130 respectively.
- SFT2A is the only row-level training recipe marked `direct_reuse`, and only in its separate
  legacy configuration: 13,367 resolved rows (307 true, 13,060 false), six unknown sidecar rows,
  and seven exact directed text-pair duplicates to remove before splitting. SFT2B is `revalidate`:
  all 301 compiled rows have unknown semantic labels and remain three-voter inputs, never negatives.
  The canonical 301 reconciles as 3 public-research + 195 Algebra + 103 cross-domain rows and
  excludes the superseded public postprocess-v1 tranche.
- The SFT2B preview joins uncovered and then explicitly bound two frozen dependencies rather than
  guessing: the 10,000-row Gate 3 input file at
  `8eb75ffa0b9233c5a91492fa181f604e3c098a6f3970799bcb0406f8b517f09e` and the 20-row
  cross-domain reference representation file at
  `072c341f52339edc56fa90f548470d22aeea3a3fcb0936ac1a1e3af3255ee39b`.
- Gold is `direct_reuse` for evaluation only; its old partition file is `legacy_reference`. Curated
  CPT is `revalidate`: all 469,585 texts are unique, but 463 legacy IDs are duplicated across 515
  excess rows, and every duplicated ID maps to distinct text. The legacy mixed corpus and prior
  screened/mixed CPT roots remain smoke/reference assets only.
- The one-example smoke traced SFT2A source record
  `recovered_judgment:000196de5c3727cc5a5caba4b3eaa8520d554c665f6fef9003886b0f9e6c31ad`
  through its pair plan, stored Lean check, judgment, stable preview ID
  `reuse_preview:906895fd3b9ddf868d34c896b3284a228f9b40998aa1bb7d1a78e668f6d3dccf`,
  and serialized minimal row. The source trainer file remained identical in size and nanosecond
  mtime before/after and retained SHA-256
  `5de1f904904da6fa204a446e65c58d137a59a6a21d5afa15eb1ad24dbf3bf2f1`.
- Verification passed: JSON config parse; Ruff format check and Ruff lint on
  `src/leanfaith/data_reuse/` and `tests/unit/data_reuse/`; strict mypy; three focused pytest tests;
  `git diff --check`; full hash/row/schema reconciliation; source before/after snapshot; and the
  deterministic second run. Lean is the bottleneck, so no Lean or LLM was invoked. No data was
  generated, relabelled, merged, uploaded, deleted, or mutated.

## Progress log (append-only)

- 2026-08-30 — task brief created; no inventory execution performed.
- 2026-08-30 — Codex `/root` claimed DATA-REUSE and its exact writable paths. Lean budget remains
  zero; first action is a read-only, hash-backed inventory. The unrelated existing change in
  `plans/02_goal_v1.md` is outside this session and will be preserved.
- 2026-08-30 — completed the read-only pass over all eight required roots and 14 additional
  evidence assets. Reconciled hashes, schemas, counts, label/checker provenance, representation
  joins, redistribution boundaries, and duplicate/ancestry overlap before writing any staging
  output; no mutable-source drift was observed.
- 2026-08-30 — emitted the 22-record inventory, 117 bounded adapter previews, evidence JSON,
  manifest, and report under the claimed staging root. A missing three-record SFT2B sample join
  failed closed, then reconciled against two additional frozen, hash-pinned source catalogs before
  the successful run.
- 2026-08-30 — one-example smoke, pilot reconciliation, focused tests, static checks, source
  immutability check, and deterministic rerun passed. Marked DATA-REUSE complete. Exact next action
  is SFT2A-owner review of the legacy recipe above; SFT1 remains waiting on explicit user approval.
- 2026-08-30 — independent review scientifically accepted all 22 artifact decisions and the
  regenerated 117-preview bundle, but found the repository freeze incomplete: owned files were
  uncommitted, Ruff formatting failed, strict mypy required three annotations, and the staged
  manifest lacked an implementation or Git identity. Reopened status as `active`; Lean budget
  remains zero while these operational blockers are repaired.
- 2026-08-30 — completed the operational freeze: applied Ruff formatting, added the three strict
  overlap-map annotations, bound the formatted implementation SHA-256 in the manifest, preserved
  all four scientific output hashes, and reproduced all five final bundle hashes on a second full
  run. Ruff format/lint, strict mypy, three focused tests, and diff checks pass. Restored status to
  `complete`; the final repository commit contains only the claimed DATA-REUSE paths.

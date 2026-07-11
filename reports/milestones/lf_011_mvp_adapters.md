# LF-011 — MVP adapters and live probe integration

**Date:** 2026-07-10
**Scope (PLAN.md §26):** mathlib, selected NL source, ProofNetVerif adapters.
Data collection approved by the operator; Gate 0 closed
(`reports/gates/gate_0.json`) with the primary source verified.

## Delivered

- **`sources/hf_sft_classic.py`** (primary NL source, verified structure at
  rev `0bf9f424…`): `unwrap_question` (lean4 fence, header lines, `/-- -/`
  docstring NL, statement fragment, truncation flag), `parse_row` →
  `ParsedRow` with explicit `parse_status` failure records, and
  `classify_trust` implementing §9.4 per row: Lean-Workbook-derived rows —
  identified by declaration name, since they keep Goedel-style uuids —
  are `synthetic` (§9.1 overlap rule); all other upstream corpora stay
  `uncertain` until provenance verification. Statement isolation stays
  LF-012's Lean-aware job.
- **`sources/proofnetverif.py`**: §9.3 mapping verbatim, evaluation-only
  records, fail-closed on missing columns.
- **`sources/mathlib.py`**: deterministic glob-scoped file inventory over the
  pinned checkout with drift refusal (`verify_checkout_revision`).
- **Config binding + CLI**: `hf_probe_config_from_yaml` binds
  `configs/sources/*.yaml` (incl. `HF_TOKEN` by name) to probe configs;
  `leanfaith probe <source|all>` runs live probes, archives manifests +
  samples, writes a run manifest, exits nonzero on any block.
  `scripts/01_probe_sources.py` wraps it (§7.2).

## Data products (local `data/`; bulk on /storage/milikic/leanfaith)

- `data/parsed/sources/proofnetverif/{valid,test}.jsonl` — full 3,752 rows
  parsed with manifest.
- `data/parsed/sources/sft_classic/train_slice_000000_020000.jsonl` +
  `pool_stats.json` + manifest — 20k-row parsed slice:
  - parse status: 7,157 `parsed` (docstring NL) / 12,843 `no_docstring`
    (proof-SFT-only rows kept as failure records);
  - trust: 11,616 `uncertain` / 8,384 `synthetic` (Workbook-derived, 42%);
  - **NL-eligible: 7,157 (35.8%), of which 7,149 non-workbook** —
    `phase5_pool_estimate_train ≈ 718k` (uncertain provenance; the exact
    filtered count runs with the full pass at LF-012/13).
- `data/source_manifests/mathlib_inventory.json` — 8,112 `.lean` files
  hashed at the pinned revision (the LF-012 extraction frame).

## Acceptance evidence

```text
ruff / mypy               → clean (39 source files)
pytest tests/unit         → all green (incl. 20 adapter tests; private-sample
                            tests auto-skip where the sample is absent)
live probes               → 7/7 archived (see reports/source_probes/)
```

## Notes / deviations

- The plan's assumption of directly separable NL held only via docstring
  unwrapping; ~64% of sft_classic rows carry no NL at all (recorded in the
  probe report). The Lean side of those rows remains usable for Lean–Lean
  pools.
- `sft_classic_numina` and `lean_workbook` full adapters follow the same
  binding path; their dedicated modules land with the Phase-5 problem-pool
  work (they are fallback/weak-supervision sources, not MVP-critical).

**Next:** LF-012 — Lean-aware declaration extraction over the mathlib
inventory and the parsed sft_classic Lean sources.

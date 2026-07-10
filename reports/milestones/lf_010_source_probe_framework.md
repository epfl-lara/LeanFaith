# LF-010 — Source probe framework

**Date:** 2026-07-10
**Scope (PLAN.md §26):** revisions/licenses/schema/sample/archive/fallback.
Framework + clients; live probes of private sources remain gated on the §9.2
approval decision (data-collection boundary).

## Delivered

- `src/leanfaith/sources/base.py`: `DatasetProbeInfo`, `ProbeOutcome`
  (accessible vs structured-blocked outcomes; inline canonical-JSONL sample
  until archival), `SourceProber` protocol, `SourceProbeError` (fail-closed:
  schema mismatch, hash mismatch, archival failure).
- `src/leanfaith/sources/probe.py`:
  - `HFDatasetProber` over a narrow `HFClient` protocol: identity/revision/
    license/gating/splits/columns; pinned-schema check (§9.3: fail closed,
    never guess); 100-row canonical-JSONL sample with SHA-256; access status
    (public vs private-authenticated); secrets resolved by env-var name at
    call time and verified absent from all dumps.
  - `RealHFClient`: lazy `huggingface_hub`/`datasets` imports; 401/403/404/
    gated → `AccessBlockedError` → structured blocked outcome (no
    fabrication, §25 item 10).
  - `archive_probe`: writes `data/source_manifests/<source>.json` (§9.5) and
    `data/raw/sources/<source>/probe_sample.jsonl`, hash-verified; blocked
    outcomes archive a blocked manifest instead.
  - `run_fallback_chain`: §9.1 fixed fallback order — first accessible
    outcome wins, blocked attempts are preserved as evidence; all-blocked
    raises.
- `src/leanfaith/sources/repository.py`: `GitRepositoryProber` over a
  `GitClient` protocol — revision reachability (ls-remote / raw-fetch for
  SHAs), checked-in `lean-toolchain` capture, §6.2 mismatch flagged in the
  manifest notes. Metadata only; cloning/extraction is Phase 2.
- Dependencies: `datasets>=3.0`, `huggingface_hub>=0.30` (runtime, lazy).

## Acceptance evidence

```text
uv run ruff check . / mypy → clean (36 source files)
uv run pytest tests/unit   → 197 passed
```

Covered failure paths: schema mismatch (fail closed), 401-blocked source →
structured block record, exhausted fallback chain, secret never stored,
sample hash determinism + pre-archival hash verification, unreachable git
revision, toolchain mismatch flagging.

## Notes / deviations

- Config binding (`configs/sources/*.yaml` → probe configs) and the
  `leanfaith probe` CLI land with LF-011 (MVP adapters), which is also where
  the *live* probes run once the user approves the data-collection step.

**Next:** LF-011 (MVP adapters + live probes) — blocked on user approval of
the authenticated `sft_classic` probe and mathlib checkout (data collection).

# Phase 0 — Contract, sources, providers, environment (Gate 0 status)

**Date:** 2026-07-10
**Decision:** Gate 0 PARTIALLY CLOSED — every item that does not require
external data access or a user decision is locked; the three remaining items
are enumerated under "Open items (data-collection boundary)" and block only
the phases that consume them.

## Locked (with artifacts)

1. **Policies** (`policies/`): semantic_policy_v1.md (all §3.6 edge cases
   decided with examples), error_ontology_v1.yaml (E01–E30),
   label_resolution_v1.yaml (§14.6 precedence, §15.7 routes, F0–F2
   derivation), transformation_promotion_v1.yaml (Gate 4A/4B numeric rules),
   benchmark_denylist_v1.yaml (§19.7 registry + freeze timing),
   evidence_policy_v1.yaml (§16), split_policy_v1.yaml (§19),
   calibration_policy_v1.yaml (§21.10/§31.5), preregistration_v1.yaml
   (H1–H6 numeric targets, Gate6/Gate7, statistics rules).
2. **Environment lock** (`configs/environment.lock.yaml`, ADR-0001,
   `leanfaith doctor --write-lock`): advertised_range mode; Lean
   `v4.31.0-rc1`; `lean-interact==0.11.4` (advertised range
   v4.8.0-rc1..v4.31.0-rc1; REPL fork augustepoiroux/repl v1.3.17); Python
   `>=3.12,<3.13`. Doctor enforces it and is fully green.
3. **Project pins** (`configs/projects/`): fixtures (local, v4.31.0-rc1,
   builds + passes live REPL tests); mathlib4 tag `v4.31.0-rc1` =
   `d568c8c09630de097a046763c17b9ea99f95f950` (toolchain matches lock
   exactly); cslib pinned to last in-range revision `2f677bfc8ef7...`
   (HEAD is on v4.32.0-rc1 — outside range, recorded); physlib pinned to
   last in-range revision `f5242c99d796...` (v4.30.0; HEAD is stable
   v4.31.0 — outside range; Phase 11 decision recorded in ADR-0001).
4. **Source identities** (`configs/sources/`): sft_classic (private;
   HF_TOKEN by name; fallback order per §9.1), sft_classic_numina
   (~99,774 rows; §9.3 mapping), Lean-Workbook (synthetic weak only),
   ProofNetVerif (frozen benchmark; §9.3 mapping), mathlib, cslib, physlib.
5. **Benchmark registry** (`configs/benchmarks/registry.yaml` +
   `policies/benchmark_denylist_v1.yaml`) registered before any generation.
6. **ADRs**: 0001 environment lock (Accepted); 0002 annotation platform
   (Proposed — Argilla default, finalizes at LF-023); 0003 data versioning
   (Accepted — manifests authoritative, DVC at research_v1, bulk storage at
   /storage/milikic); 0004 encoder/tokenizer (Proposed — ModernBERT-large
   default, finalizes at LF-028 audit).
7. **Doctor** (`leanfaith doctor`, `--write-lock`): implemented, green.

## Resolved 2026-07-10 (data collection approved by user)

1. **Authenticated `sft_classic` probe — DONE.** Revision
   `0bf9f424309f668c2c2dd214aef6ec5d1d5c042f`; 2,006,425 train + 1,029,845
   test rows; 12-column schema verified; 100-row sample archived
   (sha256 9913ae83…). NL lives in Lean docstrings of prompt-wrapped,
   proof-stripped questions; per-row `data_source` provenance is mixed —
   `nl_trust` is tagged per row by the LF-011 adapter (§9.4). All fallback
   sources and Lean projects probed and pinned; mathlib checked out with
   full build cache at `/storage/milikic/leanfaith/mathlib4`. See
   `reports/source_probes/source_probes_2026_07_10.md` and
   `reports/gates/gate_0.json`.

## Open items (need user account decisions; block Phases 5/6 only)

2. **Provider slot resolution (Phase 0 task 4).**
   `configs/generation/providers.yaml` declares the six §17.2 slots with
   family-separation rules; exact provider/model IDs, API keys (by env-var
   name), and the supervision-free primary judge family need the user's
   account decisions.
3. **§9.2 external-API approval decision.** Whether (and to which provider
   set/scope) private `sft_classic` content — including NL statements — may
   be sent. Recorded as `external_api_approved: null` until decided; all
   Phase 5/6 code checks this flag before submitting prompts.

## Consequences

Phases 1 (closed), and code-only parts of 2–4 can proceed; any step that
reads real source data (mathlib checkout/extraction, HF probes, provider
calls) waits on the open items above.

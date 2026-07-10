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

## Open items (data-collection boundary — need user action/approval)

1. **Authenticated `sft_classic` probe (Phase 0 task 3 / Gate 0 item).**
   `HF_TOKEN` is present and the probe framework (LF-010) is ready; running
   it downloads a 100-row sample and archives license/schema/counts. Blocked
   only on approval to begin data collection. Fallback order applies if the
   authenticated probe fails.
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

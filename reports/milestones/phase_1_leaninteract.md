# Phase 1 — LeanInteract backend vertical slice (Gate 1)

**Date:** 2026-07-10
**Decision:** Gate 1 PASSED (with two explicitly deferred sub-items noted below).
**Backlog items:** LF-005, LF-006, LF-007, LF-008, LF-009.

## §8.12 requirement-by-requirement evidence

| # | Requirement | Evidence |
|---|---|---|
| 1 | import/signature introspection for every Appendix A symbol | `leanfaith probe-api`; `reports/compatibility/leaninteract_api.json` (14 symbols, 7 caveats, ok=true); `tests/unit/test_api_probe.py` |
| 2 | supported/unsupported toolchain checks | `lean/versions.py` + `lean/project_registry.py`; `tests/unit/test_lean_versions.py`, `test_project_registry.py` (stable v4.31.0 rejected without ADR, v4.32.0-rc1 always rejected) |
| 3 | valid/placeholder/invalid/timeout/forced-crash/setup-error/internal-error normalization | `tests/unit/test_response_normalization.py` (all statuses); `tests/integration/leaninteract/test_backend.py` (live valid/placeholder/invalid/timeout); crash/internal paths unit-tested via exception mapping |
| 4 | per-item batch exception/order preservation | live `test_batch_preserves_order_and_isolates_items` + pooled `test_pool_batch_order_and_statuses` |
| 5 | raw response persistence and cache-key determinism | live `test_raw_response_persisted_before_normalization`, `test_request_hash_deterministic_and_allow_sorry_sensitive` |
| 6 | Command and FileCommand declaration extraction golden tests | live `test_command_declarations_golden` (committed golden `tests/golden/leaninteract/command_declaration.json`), `test_file_command_declarations` |
| 7 | explicit allow_sorry behavior | probe caveat (permissive default verified) + hash sensitivity test; every adapter call classifies placeholders explicitly |
| 8 | InfoTree none/substantive/full smoke | live `test_infotree_smoke_none_substantive_full` |
| 9 | one-worker/multiworker equivalence | live `test_one_vs_multiworker_semantic_equivalence` (identical (request_hash, status) sequences) |
| 10 | experimental auto-server recovery and stable fallback | live `test_auto_server_mode_runs_and_recovers`; constructor fallback path in `_ensure_server` |
| 11 | memory-product warning/platform handling | doctor memory-product warning (§8.7); `memory_hard_limit_mb` plumbed and probe-verified Linux-only caveat recorded |
| 12 | no LeanInteract import outside the Lean boundary | repo-wide guard `test_no_leaninteract_import_outside_backend` (static import scan) |

Additional Gate 1 requirements: one terminal ordered result per request
(§8.4) — enforced by adapter construction and batch tests; §8.4/A.5 parity —
byte-equivalence + AST-contract golden tests.

## Deviations / deferred

- Context-grouped incremental workloads (same-prefix env reuse, §8.7) are a
  Phase 2 extraction concern; the session policy covers independent batches
  only. Re-checked at LF-012.
- Golden-response upgrade diffs (§27.4) run at the first dependency upgrade;
  no upgrade has occurred under the pin.

## Environment

Lean v4.31.0-rc1 (elan), fixture project `tests/lean_fixtures` builds, REPL
fork v1.3.17 built; doctor fully green (see `reports/gates/doctor_latest.json`).

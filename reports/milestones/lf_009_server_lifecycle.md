# LF-009 — Server lifecycle

**Date:** 2026-07-10
**Scope (PLAN.md §26):** stable/experimental/pool/retry/recovery/memory checks.

## Delivered

- `src/leanfaith/lean/session_policy.py` (LeanInteract-free):
  - `ServerMode` (stable / auto / pool);
  - `RetryPolicy`: bounded attempts; only infrastructure statuses are ever
    retryable (CRASH, INTERNAL_ERROR by default; TIMEOUT opt-in); semantic
    statuses and SETUP_ERROR are rejected as retry targets at construction;
  - `run_with_retries`: full attempt lineage (`RetryOutcome.attempts`);
    each attempt carries `metadata["attempt"]`, which never enters the
    request hash but keys distinct raw artifacts (§28.4: retries append
    lineage, never overwrite);
  - `semantic_identity`: the (request_hash, status) projection used by the
    §8.7 one-vs-multiworker equivalence check.
- Backend extensions (`leaninteract_backend.py`):
  - AUTO mode uses experimental `AutoLeanServer` with tested stable-server
    construction fallback (`auto_fallback_active` flag);
  - POOL mode routes `run_batch` through `LeanServerPool` with per-item
    normalization of response/`LeanError`/`Exception` (§8.5) and order
    preservation; single `run` in pool mode uses a stable server;
  - raw artifact filenames gain `.attempt<n>` suffixes for retry lineage.
- Memory-product check (workers × per-process limit vs detected RAM) was
  delivered in the LF-007 doctor and remains a report, not a mandate (§8.7).

## Acceptance evidence

```text
uv run ruff check . / mypy → clean (32 source files)
uv run pytest              → 260 passed
```

Integration (live REPL): pool batch order + statuses over a 2-worker pool;
**one-vs-multiworker semantic equivalence** (identical (request_hash, status)
sequences); AutoLeanServer valid→timeout→recovery cycle; retry lineage with
two distinct raw artifacts and a single cache key; InfoTree none/substantive/
full smoke (§8.12 items 8–10). Unit: bounded retries, no-retry-on-semantic,
timeout opt-in, policy validation failure paths.

## Notes / deviations

- `LeanServerPool.run_batch` accepts a single `timeout_per_cmd`; the adapter
  passes the max of the batch's per-request timeouts. Per-request timeouts
  in pooled batches would require per-item submission; acceptable because
  §8.7 pools are for independent same-policy batches.

**Gate 1 status:** all §8.12 items now have executable coverage except the
context-grouped incremental workload policy (a Phase 2 extraction concern)
and golden-response upgrade diffs (exercised at the first dependency
upgrade). `reports/milestones/phase_1_leaninteract.md` will be written when
Gate 1 is formally closed alongside Phase 0 lock authoring.

**Next:** Phase 0 authoring (policies/configs/ADRs), then LF-010.

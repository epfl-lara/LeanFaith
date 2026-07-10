# LF-008 — LeanInteract adapter

**Date:** 2026-07-10
**Scope (PLAN.md §26):** Command/FileCommand/raw response/explicit
placeholder/status mapping. The LF-006 probe preceded all adapter code.

## Delivered

- `tests/lean_fixtures/`: minimal Lake project pinned to Lean `v4.31.0-rc1`
  (inside the advertised LeanInteract range; §6.2 default mode), no mathlib
  (PR-Lean CI tier, §27.2). Three clean fixture theorems plus
  `Extra/Invalid.lean` (deliberately broken, not built by lake) for INVALID
  FileCommand tests. Toolchain installed, project built, REPL
  (`augustepoiroux/repl` v1.3.17) built and exercised.
- `src/leanfaith/lean/response_normalization.py` (LeanInteract-free): §8.6
  classification (error diagnostics → INVALID; `declaration uses \`sorry\``
  → VALID_WITH_SORRY; clean → VALID), REPL `LeanError` → INTERNAL_ERROR with
  message preserved, exception mapping (TimeoutError → TIMEOUT;
  ConnectionAborted/ChildProcess/BrokenPipe/EOF → CRASH; other →
  INTERNAL_ERROR with safe type+message digest).
- `src/leanfaith/lean/leaninteract_backend.py` — the single module importing
  LeanInteract (§8.1): request validation + §8.4 request hashing, Command/
  FileCommand construction (file paths resolved against the project), raw
  response/exception persisted to `<raw_dir>/<request_hash>.json` **before**
  normalization, per-item batch isolation with order preservation,
  SETUP_ERROR on project/config failure, lazy server recreation after
  timeout/crash (the pinned server dies on timeout).

## Verified 0.11.4 behavior worth recording

- **`root_goals=True` reports root goals through the `sorries` channel and
  flips `lean_code_is_valid(allow_sorry=False)` to False even for fully
  proved code.** Real placeholders are identified by the
  `declaration uses \`sorry\`` diagnostic. Normalization keys on messages
  first; root-goal entries populate `LeanResult.root_goals` and are dropped
  from `sorries` for VALID results. (Discovered by fixture experiment;
  unit-tested.)
- On timeout LeanInteract kills the REPL process; the adapter drops and
  lazily recreates the server (integration-tested: timeout → recovery).

## Acceptance evidence

```text
uv run ruff check . / mypy    → clean (31 source files)
uv run pytest                 → 242 passed (incl. 13 Lean integration tests)
```

Integration coverage (§8.12 subset): valid/placeholder/invalid statuses,
diagnostics preserved, root-goal extraction, Command+FileCommand declaration
extraction with committed golden (`tests/golden/leaninteract/`), timeout →
recovery, ordered batch with per-item isolation, raw-response persistence,
request-hash determinism and `allow_sorry` sensitivity, pre-Lean request
validation failure.

## Notes / deviations

- `run_batch` is sequential here; pool parallelism with order restoration and
  the one-vs-many-worker equivalence test is LF-009 scope (contract already
  honored: one terminal result per request, input order).
- Golden files live under `tests/golden/leaninteract/` (a declared tree path
  that is version-controlled); `artifacts/golden/` remains for generated
  copies since `artifacts/` is gitignored.

**Next:** LF-009 — server lifecycle (pool/auto-server/retry/equivalence).

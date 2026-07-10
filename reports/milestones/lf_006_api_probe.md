# LF-006 — LeanInteract API-shape probe

**Date:** 2026-07-10
**Scope (PLAN.md §26):** introspect imports/signatures/defaults/fields; write
compatibility artifact. Phase 1 task 1; precedes all adapter code (LF-008).

## Delivered

- `src/leanfaith/lean/api_probe.py`: executable probe of the pinned
  `lean-interact==0.11.4` distribution covering every Appendix A.2 symbol plus
  the §8.3 project abstractions (14 symbols) and 7 pinned caveats:
  1. `lean_code_is_valid` default **is** permissive (`allow_sorry=True`) —
     confirming §8.5; every LeanFaith call must pass it explicitly;
  2. `LeanServerPool.run_batch` returns response/`LeanError`/`Exception`
     per item (return annotation verified);
  3. `LeanREPLConfig(memory_hard_limit_mb=...)` supported;
  4. `LeanServer.run(request, timeout=...)` supported;
  5. `Command` exposes `cmd/env/declarations/root_goals/infotree`
     (`rootGoals` is only the wire alias — A.4 confirmed);
  6. `FileCommand` exposes `path/declarations`;
  7. `AutoLeanServer` present (experimental; stable fallback mandated).
- Verified interface types import from `lean_interact.interface`
  (`CommandResponse`, `DeclarationInfo`, `InfoTreeOptions`, `LeanError`), not
  top level (§34.3).
- REPL fork identity: `https://github.com/augustepoiroux/repl`, default
  REPL version `v1.3.17` (recorded per §34.2).
- CLI `leanfaith probe-api`: writes
  `reports/compatibility/leaninteract_api.json` (committed deliverable) and
  `artifacts/compatibility/leaninteract_api_0.11.4.json`, plus a
  `runs/<run_id>/manifest.json` run manifest (artifact_class=diagnostic);
  exits nonzero on any missing symbol/failed caveat.

## Acceptance evidence

```text
uv run leanfaith probe-api   → LeanInteract 0.11.4 API probe OK (14 symbols, 7 caveats)
uv run ruff check .          → All checks passed!
uv run mypy                  → Success: no issues found in 26 source files
uv run pytest                → 174 passed
```

## Notes / deviations

- The probe loads LeanInteract dynamically via `importlib` and inspects it as
  data; it issues no semantic Lean operations. The §8.12-item-12 static-import
  guard (only `leaninteract_backend.py` may `import lean_interact`) remains in
  force and tested.
- The advertised Lean range is not a package constant; it is recorded from the
  §6.2 pin and stored in the report for the doctor (LF-007) to enforce.

**Next:** LF-007 — project/context registry and doctor checks.

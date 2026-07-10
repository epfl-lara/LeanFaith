# LF-007 — Project/context registry and doctor

**Date:** 2026-07-10
**Scope (PLAN.md §26):** supported-range/toolchain/revision/context hash doctor.

## Delivered

- `src/leanfaith/lean/versions.py`: Lean toolchain version parsing
  (`v4.X.Y[-rcN]`, `leanprover/lean4:` prefix) with the binding ordering rule
  rc < stable, and the advertised LeanInteract range as an executable
  constraint: stable `v4.31.0` is **outside** `v4.8.0-rc1..v4.31.0-rc1`;
  `v4.32.0-rc1` (mathlib master) is always rejected (§6.2).
- `src/leanfaith/lean/project_registry.py`:
  - `ProjectSpec` (configs/projects/<key>.yaml; key must match file stem),
    `EnvironmentLock` (B.1 shape: python, lean_interact pin + advertised
    range + repl fork, toolchain_lock, lean_backend settings);
  - toolchain enforcement: `read_project_toolchain` (checked-in
    `lean-toolchain`), `check_toolchain_allowed` (in-range default; stable
    v4.31.0 only under `stable_v4_31_exception` mode **with** a recorded
    ADR-0001 reference), `check_project_toolchain` (pin match + lock match;
    no silent overrides);
  - context identity (§8.11/§12.5): `ContextPayload` (environment schema,
    Lean/LeanInteract/REPL versions, project URI/revision, imports,
    namespace/open/scoped context, options, notation, header),
    `context_fingerprint` = SHA256(canonical payload),
    `build_context_record` emitting the §11.2 record with
    `context_id = "ctx:"+fingerprint`.
- `src/leanfaith/cli/doctor.py` + `leanfaith doctor`: checks Python 3.12
  window, lean-interact pin, LF-006 probe report ok, elan/lake presence
  (sanctioned §34.6 diagnostic shell), environment lock resolution and
  pin agreement, per-project toolchain validation, memory-product
  warning (workers × per-process limit vs detected RAM — §8.7 safety report,
  not a mandate). Writes `reports/gates/doctor_latest.json`; exit 1 on any
  failed check.

## Acceptance evidence

```text
uv run ruff check .          → All checks passed!
uv run mypy                  → Success: no issues found in 29 source files
uv run pytest                → 214 passed
```

Failure paths tested: missing/unparsable toolchain file, stable-v4.31.0
without ADR, v4.32.0-rc1 under both modes, pin/lock mismatches, registry
stem mismatch, missing environment lock, missing probe report, doctor CLI
exit codes on pass/fail roots.

## Notes / deviations

- `leanfaith doctor --write-lock` (Phase 0 task 8) is deferred to the Phase 0
  authoring wave, which creates `configs/environment.lock.yaml`; the doctor
  currently *reads* and enforces the lock.
- The doctor reports a missing local checkout as a warning, not an error, so
  it can run before mathlib/CSLib/Physlib are cloned (they are pinned but not
  yet fetched at this stage).

**Next:** LF-008 — LeanInteract adapter with fixture Lean project.

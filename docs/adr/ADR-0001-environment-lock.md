# ADR-0001: Environment lock (Lean toolchain, mathlib pin, LeanInteract versions)

**Status:** Accepted
**Date:** 2026-07-10
**Source of truth:** PLAN.md sections 6.1, 6.2, 8.1, 8.2, Appendix B.1. This ADR records
the Phase 0 toolchain-mode choice that PLAN.md section 6.2 requires; the executable lock
lives in `configs/environment.lock.yaml` and per-project pins in `configs/projects/*.yaml`.
This ADR is the human-readable rationale, not a second machine-readable copy.

## Context

All production Python-to-Lean interaction goes through `lean-interact` (PLAN.md
section 6.3), so the LeanInteract compatibility range is a binding constraint on every
toolchain in the project. The verified facts:

- `lean-interact==0.11.4` advertises support for Lean `v4.8.0-rc1` through
  `v4.31.0-rc1` and wraps the maintained REPL fork
  `https://github.com/augustepoiroux/repl` (default REPL version v1.3.17). The upstream
  REPL API/wire format must not be assumed interchangeable (PLAN.md section 8.1).
- Stable Lean is `v4.31.0`, which is OUTSIDE the advertised maximum `v4.31.0-rc1`.
- mathlib master has moved to a `v4.32.0-rc1` toolchain and must never be used while it
  requires `v4.32.0-rc1` (PLAN.md section 6.2, lines 335-345).
- mathlib4 tag `v4.31.0-rc1` resolves to commit
  `d568c8c09630de097a046763c17b9ea99f95f950`, whose checked-in `lean-toolchain` is
  exactly `leanprover/lean4:v4.31.0-rc1` (verified match, no override needed).

PLAN.md section 6.2 requires Phase 0 to choose exactly one coherent toolchain mode:
advertised-range mode (default) or a stable-`v4.31.0` exception mode that is permitted
only after the complete section 8.2 compatibility probe passes and this ADR records the
out-of-range status. Silent `lean-toolchain` overrides, mixing stable `v4.31.0` with an
RC-pinned mathlib environment without a tested migration, and floating branches in
research runs are all forbidden.

## Decision

decision: **advertised_range mode.** The environment lock pins:

| Component | Pin |
|---|---|
| toolchain mode | `advertised_range` |
| accepted Lean | `v4.31.0-rc1` (inside the advertised range, at its maximum) |
| mathlib4 | tag `v4.31.0-rc1` = commit `d568c8c09630de097a046763c17b9ea99f95f950` |
| mathlib `lean-toolchain` | `leanprover/lean4:v4.31.0-rc1`, matches the accepted Lean exactly (`mathlib_toolchain_must_match: true`) |
| lean-interact | `==0.11.4` (exact pin in `pyproject.toml` / `uv.lock`) |
| REPL identity | fork `https://github.com/augustepoiroux/repl`, default v1.3.17 as shipped by lean-interact 0.11.4 |
| Python | `>=3.12,<3.13` (reference 3.12.x, PLAN.md section 6.1) |

The generated `configs/environment.lock.yaml` (Appendix B.1 schema,
`environment_schema_version: 1`) carries these values with
`toolchain_lock.mode: advertised_range`, `toolchain_lock.accepted_lean: "v4.31.0-rc1"`,
and `toolchain_lock.stable_v4_31_exception_adr: null`. `leanfaith doctor` rejects any
project toolchain outside the advertised range, any mathlib `lean-toolchain` mismatch,
and any use of mathlib on `v4.32.0-rc1`.

## Consequences

1. **Stable v4.31.0 is excluded.** Any future use of stable Lean `v4.31.0` requires
   reopening this ADR under the full section 8.2 procedure: dedicated lock change,
   API-shape diff, golden-response diff, 1,000-record extraction/evidence comparison,
   explicit cache/schema migration decision, and Gate 1 testing of the exception. Until
   then the doctor treats `v4.31.0` as out of range.
2. **cslib.** HEAD `c0120dddfe75d4ab913691c0c184fc436927b19d` is on `v4.32.0-rc1`,
   outside this lock, and cannot be extracted under it. Phase 11 preparation (LF-030)
   must pin the last in-range revision `2f677bfc8ef7` (`v4.31.0-rc1`, 2026-05-29) in
   `configs/projects/cslib.yaml`, or reopen this ADR. The Phase 2 probe records this
   revision/toolchain evidence.
3. **physlib.** No recent in-range revision exists: HEAD `b0070e4bfd04` is on stable
   `v4.31.0` (outside the advertised range) and the older revision `f5242c99d796` is on
   `v4.30.0` (in range but older content).
   unresolved: physlib pin is deferred to Phase 11 preparation (LF-030) and recorded
   here as an open consequence. It is unblocked by either (a) pinning `f5242c99d796`
   (`v4.30.0`) in `configs/projects/physlib.yaml`, accepting older content, or
   (b) reopening this ADR for a stable-`v4.31.0` exception after the complete
   section 8.2 probe. Until resolved, physlib remains probe-only
   (`role: probe_now_adapter_at_ood`) and emits no extraction records.
4. **Upgrades.** Any change to `lean-interact`, the accepted Lean, or the mathlib pin is
   a lock change under section 8.2 and reopens this ADR; there is no in-place bump.
5. **Single authority.** `configs/environment.lock.yaml` is the executable lock;
   `configs/projects/*.yaml` hold per-project revisions/toolchains/root modules. If this
   ADR and those files ever disagree, the lock files are wrong and must be regenerated,
   or this ADR must be superseded; code never reads versions from this document.

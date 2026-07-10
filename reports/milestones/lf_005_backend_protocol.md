# LF-005 — Backend protocol (Appendix A.5)

**Date:** 2026-07-10
**Scope (PLAN.md §26):** exact Appendix A.5 contract; no LeanInteract imports
above the adapter. LeanInteract-free, so it precedes the LF-006 probe.

## Delivered

- `src/leanfaith/lean/protocol.py`: `LeanStatus`, `LeanRequest`, `LeanResult`,
  `LeanBackend` reproducing the Appendix A.5 code block (AST-identical;
  verified by golden test). Helpers outside the canonical block:
  - `validate_request`: §8.4 invariants — exactly one of code/file_path,
    positive timeout, nonempty request/context IDs;
  - `compute_request_hash`: §8.4 hash covering payload, context
    (ID + fingerprint), timeout, `allow_sorry`, InfoTree level, method
    version, and `environment_schema_version`; excludes `request_id`/
    `metadata` so cache keys identify computations, not submissions (§16.9).
- Tests:
  - PLAN.md §8.4 and Appendix A.5 python blocks are byte-equivalent (the
    LF-005 parity check named in §8.4);
  - every canonical class in `protocol.py` is AST-identical to the A.5 block;
  - repo-wide guard: no `lean_interact` import outside
    `src/leanfaith/lean/leaninteract_backend.py` (§8.12 item 12) — this test
    keeps guarding as later items land;
  - `LeanStatus` matches the §8.6 table exactly;
  - request-validation failure paths and hash coverage/exclusion tests.
- `pyproject.toml`: narrowly scoped mypy override for
  `leanfaith.lean.protocol` only (`disallow_any_generics=false`) because the
  canonical block uses bare `tuple[dict, ...]`; everything else stays strict.

## Acceptance evidence

```text
uv run ruff check .          → All checks passed!
uv run ruff format --check . → all files formatted
uv run mypy                  → Success: no issues found in 25 source files
uv run pytest                → 166 passed
```

## Notes / deviations

- The canonical dataclasses carry no inline validation (keeping them
  AST-identical to A.5); the §8.4 invariants are enforced by
  `validate_request`, which the adapter (LF-008) must call on every request.

**Next:** LF-006 — executable LeanInteract API-shape probe.

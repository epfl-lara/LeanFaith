# LF-016 — Transformation protocol, registry, and promotion boundary

**Date:** 2026-07-23  
**Decision:** complete  
**Scope:** infrastructure only; zero theorem variants generated

## Authorization and immutable prerequisites

LF-016 began only after the existing authorization passed its hash-verified
preflight:

| Artifact | SHA-256 |
|---|---|
| `reports/gates/lf_016_authorization.json` | `38350f9ccfb9b68490d267f87c44258992331bff4c55f09b48565105b801a344` |
| `reports/gates/gate_2.json` | `4874318bc44092ddb3afb906b10576adcbdecdebca20324080ae29db454c7638` |
| `reports/gates/gate_3.json` | `ef98fe2f3d45b6deb2c80d6c0c80d2f1c1b6b773ac58f08306f34c03faed2f44` |
| active benchmark-signature manifest | `4ffbd31dd0e10efb9dfe7e57fb815690f3e1d750e4640aeff959ca4cbdc911df` |

The validation command rechecked these bindings and the active benchmark
registry. Historical Gate-2 and Gate-3 reports were not modified.

## Implemented contract

- `src/leanfaith/transforms/protocol.py` defines the typed rule protocol and
  deterministic factories/verifiers for attempts, drafts, and audits.
- `src/leanfaith/transforms/registry.py` provides strict registry/profile
  loading, effective hashing, code-owned implementation keys, explicit
  pending/available/disabled status, fail-closed dispatch, persistent
  non-applicability attempts, and source/representation/context checks.
- `src/leanfaith/transforms/promotion.py` recomputes positive audits, exact
  two-sided 95% Clopper–Pearson lower bounds, all eight positive item
  conditions, and the four exactly-one negative promotion routes.
- `src/leanfaith/schemas/variant.py` now preserves complete deterministic
  lineage across attempts, drafts, audits, variants, and immutable family
  decisions. Mechanical audit recommendations remain `provisional` or
  `unknown`; intentions cannot create semantic labels.
- `configs/transformations/registry.yaml` registers nine v1 families/rules.
  The eight LF-017/LF-018 families are `experimental` with implementations
  explicitly `pending`; `p00_cosmetic` is disabled.
- `configs/transformations/v1.yaml` is the strict v1 execution profile.
- `leanfaith generate-deterministic --validate-only` and
  `scripts/05_generate_deterministic.py --validate-only` validate/freeze the
  framework and write a diagnostic run manifest while reporting exactly zero
  drafts. Normal generation refuses until scoped rule implementations exist.

No YAML value is dynamically imported as Python code. A runtime rule must be
registered by code and match the registry's family, rule version, polarity,
and implementation key before any rule method can run.

## Promotion safeguards

- Positive family promotion requires a frozen blinded design, at least 200
  eligible audited outputs, point precision at least 0.99, exact lower bound
  at least 0.95, exact per-item invariant coverage, held-out checks, and no
  recurrent E25 semantic erasure.
- Ambiguous, unresolved, policy-violating, and incorrect outputs remain in
  the precision denominator; only explicitly non-elaborated outputs are
  excluded by the registered definition.
- Negative items require exactly one accepted §15.7 evidence route.
  `not_found`, `not_proved`, vacuity/ex-falso, compilation success, mutation
  confidence, and intentions are rejected as promotion evidence.
- Promotion creates hash-bound decision records and never rewrites a
  `VariantRecord`.

The exact interval implementation was independently compared with SciPy beta
quantiles on fixed cases and 120 randomized cases through `n=20,000`; maximum
absolute error was `4.65e-14`.

## Frozen LF-016 validation artifacts

| Artifact | SHA-256 |
|---|---|
| promotion policy file | `e41064eb4a6572d0283821ce7b9be21d211d8f149afe00d62e3e241446941cf3` |
| registry YAML file | `e572d00452a7bcbcd127182f0365f3191b8808f033ed2066726201358aa359cf` |
| v1 profile YAML file | `9396c50fc088384e4085d95841f1c38ef898f3348580b4be15584e4edc03c2da` |
| effective registry | `a68ac07c0a3bd0defa7c9a7c43af1e9a28fcd5f9c80206ce3951281cbf64016d` |
| registry snapshot | `fa0c924f7b7af7cf1b7b2931818d87e46edd572b26857f89118faf73405eaabf` |
| validation report | `e02ee9b50fa79c809e8edae8174f959d44c95209f331b2cf59b778b4c72cf553` |
| diagnostic run manifest | `9d083733424faf156170944fa36b438064724d7ebcbdc5fae2fd7081330f075b` |
| doctor report | `587fd3dae6c251c31244deb429d437dc241b94a8695ddb9b0485df7240dca706` |

The file hashes and canonical effective hashes intentionally have different
roles: exact bytes are bound for artifact replay, while effective hashes bind
validated configuration with defaults and policy semantics.

## Verification

- `uv run pytest -q`: 600 tests passed.
- Focused transformation suite: 79 tests passed.
- `uv run ruff check .`: passed.
- `uv run ruff format --check .`: passed.
- `uv run mypy`: 64 source files, zero issues.
- `uv run leanfaith doctor`: seven checks passed, zero failures/warnings.
- `lake build` in `tests/lean_fixtures`: passed.
- Validation-only command: passed with `generated_drafts=0`.
- Structured tamper/failure tests: passed.

## Boundary and next item

LF-016 is complete. LF-017 and LF-018 rule semantics have not begun in this
milestone, and no deterministic research-data partition exists yet.

Gate 4G remains open until the scoped positive and negative families are
implemented, re-elaborated, audited, and provenance-complete. Gates 4A and 4B
remain open until their later annotation/evidence prerequisites are met.

**Authorized next:** LF-017 — P01/P02/P04-lite scoped positive rules.

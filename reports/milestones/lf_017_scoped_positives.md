# LF-017 scoped positive transformations

**Status:** complete  
**Date:** 2026-07-23  
**Scope:** P01 alpha renaming, P02 typed-binder regrouping, and P04-lite
finite notation/direct-form identity rewrites.

## Outcome

All three Revision 4.1 positive families now have strict, code-owned,
deterministic implementations:

- `p01_alpha` performs capture-avoiding renaming across supported Lean binder
  scopes. It requires source/candidate re-elaboration, identical
  binder-normalized identity fingerprints and semantic atoms, plus an exact
  inverse trace.
- `p02_binders` only splits or merges typed binder groups while preserving the
  delimiter kind. The v1 implementation explicitly disables currying and
  uncurrying. It requires identical elaborated binder-dependency graphs,
  fingerprints, atoms, and an exact round trip.
- `p04_notation_lite` uses only the versioned `Nat ↔ ℕ` and `Int ↔ ℤ`
  table. It requires exact source/candidate identity across the alpha
  fingerprint, explicit signature, semantic atoms, and operator tree in the
  same Lean context.

The static factory in `src/leanfaith/transforms/factory.py` is the only
configuration-to-code boundary for positive rules. It constructs no dynamic
imports, binds every implementation to the effective registry hash, rejects
unknown available keys or metadata mismatches, and registers exactly P01,
P02, and P04-lite.

Every generated item remains `provisional`. An intended `equivalent`
relation is provenance only; LF-017 creates no resolved semantic label and
performs no family promotion. Any elaboration, lineage, context, trace,
representation, fingerprint, dependency, atom, or structural mismatch is
quarantined.

## Mechanical evidence

Focused validation:

```text
uv run pytest \
  tests/unit/test_p01_alpha.py \
  tests/integration/leaninteract/test_p01_alpha_live.py \
  tests/unit/test_p02_binders.py \
  tests/integration/leaninteract/test_p02_binders_live.py \
  tests/unit/test_p04_notation_lite.py \
  tests/integration/leaninteract/test_p04_notation_lite_live.py \
  tests/unit/test_positive_rule_factory.py \
  tests/unit/test_transform_registry.py -q
```

Result: **140 passed**.

The live matrix independently elaborates source and candidate declarations
through LeanInteract. It covers shadowing, dependent binders, instances,
quoted identifiers, explicit/implicit/strict-implicit regrouping, all four
notation directions, and an unavailable-notation rejection. Adversarial unit
tests cover capture, stale/tampered traces, malformed syntax, representation
lineage and text mismatches, exact expected-diff drift, proof-free identity,
and registry fail-closed behavior.

Static checks:

```text
uv run ruff check <LF-017 implementation and test paths>
uv run ruff format --check <LF-017 implementation and test paths>
uv run mypy \
  src/leanfaith/transforms/p01_alpha.py \
  src/leanfaith/transforms/positives \
  src/leanfaith/transforms/factory.py
```

Result: passed.

The reproducible implementation inventory was emitted by:

```text
uv run leanfaith generate-deterministic --validate-positives
```

Artifacts:

- report:
  `reports/transformation_audits/lf017_positive_validation.json`
  (`sha256:7b1f7aa949e7f269c54c2a36b350f56428ef10f945c0f2a25f3893ca57ac7fdd`);
- run manifest:
  `runs/run_20260723T170002Z_604fb142/manifest.json`
  (`sha256:61adfb57275d7d1b04afb185f3e072315b0583d2268a13e1361eaef30080b024`).

The report binds:

- P01 config:
  `686b7c057ff2058b64e2ad392450c77a035a2c8502c7696b12d1ea24e173bd4b`;
- P02 config:
  `4357c8cc6fc868ce54e9ea3e34221c87b7d78fbce8f03d5dda2a1a82a17ca975`;
- P04-lite config:
  `481fce4754cf7eea28e161d42f9abd278425bfdc4e6a8f188f1ff6d10ff7468f`;
- effective registry:
  `fe6ce9ea3f2e71e00f739447c748cd6d1b743d28caa757a1c4975a90c55f41b2`.

## Gate status and next work

LF-017 is complete. Gate 4G remains open: its scope also requires all LF-018
negative families, integrated candidate re-elaboration/persistence, terminal
attempt accounting, and dual ancestry for N10. Gate 4A remains open pending
the required blinded human audit; nothing here is automatically gold.

The next ordered item is LF-018. N01 and N02 implementation began only after
Gates 2 and 3 were closed and is kept separate from this positive-family
milestone.

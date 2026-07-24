# LF-018 scoped negative transformations

**Status:** complete  
**Date:** 2026-07-23  
**Scope:** N01 operator replacement, N02 quantifier replacement, N03
independent-hypothesis deletion, N07 literal/bound replacement, and N10
nearby-theorem component replacement.

## Outcome

All five Revision 4.1 negative families have strict, code-owned,
deterministic implementations:

- `n01_operator` performs only curated type-compatible operator replacements.
- `n02_quantifier` performs exact supported quantifier-token replacements.
- `n03_drop_hypothesis` removes only an explicitly represented independent
  proposition hypothesis after rechecking the elaborated dependency structure.
- `n07_literal_bound` performs curated exact literal changes at supported
  comparison-bound sites.
- `n10_nearby_theorem` uses a distinct same-context donor theorem and preserves
  both source theorem IDs, both representation IDs, and both root ancestries.

N01/N02/N03/N07 are registered through the static unary negative factory.
N10 is constructed through a separate code-owned `PairTransformationRule` and
cannot enter the unary registry. All implementations are bound to the effective
registry plus their exact rule configuration and the shared replacement table.

Every output remains `provisional`. `near_miss` is generation provenance only.
LF-018 produces no resolved semantic labels, negative gold labels, or family
promotions. Failed proof search is not consulted as negative evidence.

## Persisted pre-scale slice

The authoritative Lean-backed run was:

```text
uv run leanfaith generate-deterministic --run-negative-pre-scale
```

It processed one configured case per family and persisted:

- 6 source theorem and 6 source representation records;
- 5 terminal generated attempts and 5 drafts;
- 5 re-elaborated candidate theorem and representation records;
- 5 mechanical audits;
- 5 provisional variants;
- 5 unresolved theorem pairs;
- an explicit empty failure partition;
- the canonical fixture `ContextRecord`;
- raw LeanInteract responses, an output manifest, and a run manifest.

Artifacts:

- pre-scale report:
  `reports/transformation_audits/lf018_pre_scale/run_20260723T174912Z_e1ef42e8.json`
  (`sha256:8809783669372977d9d9bc0479962183ebc8f99390cdc2a6f5993d284b5623ea`);
- output manifest:
  `data/generated/deterministic/lf018_pre_scale_v1/run_20260723T174912Z_e1ef42e8/manifest.json`
  (`sha256:161f6cebeedd190a339ab974ae2e71f205e28705019490442b6174055ef829ed`);
- run manifest:
  `runs/run_20260723T174912Z_e1ef42e8/manifest.json`
  (`sha256:ed987e809a03c30f2e4f189233bbb34d42712d71d9c7a86707ed5c2087157ee4`);
- implementation-inventory report:
  `reports/transformation_audits/lf018_negative_validation.json`
  (`sha256:746a7c68a5033d1842ba38b204cc0d64c6b1fce91765d861ab5da8abe1d4aee7`).

The pre-scale report records all eight mechanical checks as passing:

```text
all scoped families executed
all source/candidate views Lean-backed
all candidate statements re-elaborated
complete attempt→draft→audit→variant→pair lineage
N10 dual-source ancestry persisted
all outputs provisional
zero resolved semantic labels
zero promotions
```

The run uses immutable run-scoped output/report paths. It binds one canonical
toolchain/project/context record, one pre-write code-state snapshot, the
authorization and active benchmark manifests, the environment lock, all five
rule configs, and the shared replacement table. A repeated run cannot overwrite
files referenced by an earlier run manifest.

## Verification

The full repository verification passed:

```text
uv run pytest -o addopts=''
934 passed in 40.55s

uv run ruff check .
passed

uv run ruff format --check .
156 files already formatted

uv run mypy
Success: no issues found in 81 source files

uv run leanfaith doctor
all checks passed

(cd tests/lean_fixtures && lake build)
Build completed successfully
```

The test suite includes adversarial lineage/config/hash/tamper cases, live
LeanInteract tests for every family, immutable-output checks, and one complete
five-family persisted-artifact linkage test.

## Gate status and next work

LF-018 is complete. Gate 4G remains open because it is phase-wide: LF-017's
validation artifact intentionally generated zero records, so an integrated run
must still materialize and re-elaborate P01, P02, and P04-lite alongside the
five LF-018 negatives. Gates 4A and 4B also remain open; no automatic promotion
has occurred.

The next ordered backlog item is LF-019, the smoke vertical slice. Its
integrated deterministic generation fixture must cover all eight active
families before Gate 4G can close.

# LF-020 — Symbolic evidence pipeline

**Status:** complete; accepted smoke-only semantic replay  
**Date:** 2026-07-23  
**Scope:** evidence collection and certificate auditing only

## Intended outcome

LF-020 establishes the versioned, cached Lean evidence boundary used by later
label resolution. For every supplied Lean–Lean pair, the pipeline attempts five
terminal jobs:

```text
definitional equality
directional proof search A → B
directional proof search B → A
explicit claim alignment
bounded counterexample search
```

Successful proof and claim-alignment certificates receive separate transitive
dependency and axiom audits. The pipeline persists evidence and enriched pair
links, but it does not resolve labels or promote transformation families.
Failed proof search and absent counterexamples remain unknown evidence; they
never become semantic negatives.

## Implementation boundary

The accepted implementation provides:

- a LeanInteract-only symbolic evidence collector;
- proof-free fresh aliases for both compared statements;
- strict rejection of `sorry`, unresolved metavariables, forbidden axioms,
  source theorem constants, and candidate theorem constants;
- definitional-equality, directional proof-search, explicit claim-alignment,
  and kernel-`decide` counterexample adapters;
- `native_decide` disabled by policy;
- immutable cache keys binding statements, representations, contexts,
  policies, method/config versions, Lean revisions, generated request/code,
  dependency reports, and raw artifacts;
- five terminal evidence records per input pair, plus an axiom-audit record for
  each accepted certificate;
- a stable `leanfaith collect-evidence` command and
  `scripts/08_collect_symbolic_evidence.py` entry point;
- smoke-to-production rejection and preservation of upstream evidence and
  preexisting label links; and
- a self-hashed semantic replay comparator that checks pair semantics,
  terminal jobs, audits, cache keys, cache payloads, execution hashes,
  accounting closure, and the absence of new labels or promotions; and
- content-addressed artifact and cache catalogs whose exact membership, paths,
  and hashes are independently revalidated during replay.

The explicit alignment fixture is
`tests/fixtures/evidence/lf019_alignment_specs.jsonl`. It exercises the one
currently authorized binder-aligned alpha-identity certificate. All other
alignment attempts fail closed as unsupported.

## Bound inputs and configuration

Both accepted runs consume the immutable LF-019 Run B partitions under:

```text
data/generated/deterministic/lf019_smoke_v1/run_20260723T182826Z_467c0f50/
data/evidence/lf019_smoke_v1/run_20260723T182826Z_467c0f50/evidence.jsonl
```

The shared captured code-tree hash is:

```text
d3e1ad06ed3e902cdd182045a4ea59e2766ab1919d86ef8e9f2cf262ca056ba1
```

The bound configuration and policy SHA-256 values are:

| Artifact | SHA-256 |
|---|---|
| `configs/evidence/portfolio_v1.yaml` | `d7194e5462fbe4dc49278ccf036c87d9489c469ff74ab236746bedb8d8b4ffd5` |
| `configs/evidence/counterexample_v1.yaml` | `5809ad410850fca843e04a56590326557be06056647694dab8f670d0dd450d6d` |
| `configs/evidence/sampling_v1.yaml` | `45c546c87e461e89b1214a6e996eab8f359f7e9d02ec04ebfec0a6a7857a1e18` |
| `policies/evidence_policy_v1.yaml` | `0eedff288b4bb2b31f4ec60ba2690432d345c1bf643a909ea2581a74cb2b4b82` |
| `policies/semantic_policy_v1.md` | `d555de3b9eba9e90cb44bf8595f97684ced7098e699f901a7cba0e2805c9fd78` |

## Accepted clean-cache replay

The two runs used separate empty caches. Each cache was persisted as
`cache_snapshot` below its run artifact directory before comparison.

Run A:

- run ID: `run_20260723T193914Z_f25ec544`;
- output:
  `data/evidence/lf020_symbolic_v1/run_20260723T193914Z_f25ec544`;
- output-manifest SHA-256:
  `da5a8b5ab5e3c28f94ab4681aa5e78527a8edc1fe584f42c59c1cfa6f471b2ab`;
- run-manifest SHA-256:
  `96eb48d72ae3a41e21cf39abf34177b0150491b006ac8c74fcd4959bc6acbf8e`;
- artifact-catalog SHA-256:
  `9ff950e93296fe2cadcd1386efefad0b5eb4e2203407ff72eea2fc22f36fe7ea`;
- cache-catalog SHA-256:
  `87117de6056ffce83da81aa9bb54bf782fca6f44dd316e06d51dc7d557ca6a78`;
- artifact-catalog entries: 103; and
- cache entries: 40.

Run B:

- run ID: `run_20260723T193933Z_fa0c67d0`;
- output:
  `data/evidence/lf020_symbolic_v1/run_20260723T193933Z_fa0c67d0`;
- output-manifest SHA-256:
  `461ea873306e438e9ac5cefcf535c8c5d02b5c1d986c612ceb79931208d6744d`;
- run-manifest SHA-256:
  `88cd6c457ee23cc2c23a618533076223757a4bfb3f1c090a1ca1c9708457bcf4`;
- artifact-catalog SHA-256:
  `a4b091ee21e3702290879b25669974d2caf70adead70758a061609a7aa15da6f`;
- cache-catalog SHA-256:
  `15d6669234fb263c079014f2d804d8053689b097881e501b811c9a00fbeb772c`;
- artifact-catalog entries: 103; and
- cache entries: 40.

The canonical audit is
`reports/evidence/lf020_smoke_replay_v1.json`:

- report self-hash:
  `928a8629cbe7675d6ca180b35cc3a7be1adc49b7ce8b692b6bce7869636b61e9`;
- report-file SHA-256:
  `9216a5d4bb46563915a9fdd3aad0ae54f61cb426d4da4759332c1cfc80437093`;
- source-pair fingerprint:
  `4bec9b7d52779f88896cd2750d3ee83ee9f8aa59fb43b1582901918e4133db11`;
- upstream-evidence-ID fingerprint:
  `cc641bcf288d3559ceb253579b86d9fe814524e9daa66b0ea046be860ac0fd56`;
- semantic fingerprint:
  `fc28c80e78a4938b99645725175a8163c86934ed12a981b45acfe4cfc594f7ff`;
- cache fingerprint:
  `70c2bb639fffd27838eaccbc91de87d4a0b75ba3a756bfff954abb326a0f467a`.

Every replay check passed. Both runs contain 8 enriched pairs, 40 terminal
jobs, 9 axiom audits, 49 new evidence records, 8 resolved upstream evidence
links, and zero collection failures, unresolved links, unreferenced new
evidence records, label/promotion violations, or newly created labels.
Replay also validated the exact 103-entry artifact catalog and the exact
40-entry cache catalog and cache snapshot for each run. Missing, extra, or
tampered files fail closed.

## Observed execution measurements

Measurements are actual run observations rather than configured estimates:

| Measurement | Run A | Run B |
|---|---:|---:|
| wall time | 4.998 s | 4.944 s |
| Lean backend calls | 70 | 70 |
| Lean request attempts | 95 | 95 |
| unique Lean request hashes | 68 | 68 |
| Lean backend elapsed time | 4,540 ms | 4,484 ms |
| retries | 0 | 0 |
| cache hits / misses / puts | 0 / 40 / 40 | 0 / 40 / 40 |
| pairs per second | 1.601 | 1.618 |
| evidence records per second | 9.803 | 9.911 |

## Observed evidence outcomes

The identical result in each run is:

| Evidence kind | Outcomes |
|---|---|
| definitional equality | 3 equal; 5 not equal |
| A implies B | 3 proved; 5 not proved |
| B implies A | 4 proved; 4 not proved |
| claim alignment | 1 certified; 7 unsupported |
| counterexample | 8 unsupported |
| axiom audit | 9 successful; 0 violations |

There are 34 successful and 15 unsupported evidence records. The one
preexisting provisional P01 smoke label link is preserved byte-for-byte; no
other pair receives a label. No output label or promotion partition exists.

## Verification

The final repository checks passed:

- `uv run pytest`: 1,077 passed;
- `uv run ruff format --check .`: 193 files already formatted;
- `uv run ruff check .`: passed;
- `uv run mypy`: 103 source files passed;
- `uv run leanfaith doctor`: all checks passed;
- `lake env lean LeanFaith/Meta/ProofChecks.lean`: passed;
- fixture-project `lake build`: 5 jobs completed successfully; and
- `git diff --check`: passed.

LF-020 is complete. Gates 4A and 4B remain open, and the collected evidence
does not itself authorize any semantic label or transformation-family
promotion. LF-021 real-output collection is next.

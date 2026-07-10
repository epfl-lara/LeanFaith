# LF-002 — Config loader

**Date:** 2026-07-10
**Scope (PLAN.md §26):** strict schemas, hashes, secret references (including `HF_TOKEN`), unknown-key failure.

## Delivered

- `src/leanfaith/config/hashing.py`: canonical JSON (§11.12 discipline — sorted
  keys, compact separators, UTF-8; rejects non-finite floats, non-string keys,
  and non-JSON objects such as datetimes/paths/bytes with a JSONPath-style
  error location), `sha256_hex`, `hash_canonical`, streamed `hash_file`.
- `src/leanfaith/config/models.py`: `StrictModel` (Pydantic v2,
  `extra="forbid"`, frozen, validated defaults) — the base for every persisted
  schema; `SecretRef` referencing secrets by environment-variable name only
  (`^[A-Z][A-Z0-9_]*$`), resolving at call time, raising `MissingSecretError`
  for unset/empty values; secret values never enter models, dumps, or hashes.
- `src/leanfaith/config/loading.py`: strict YAML loading (SafeLoader subclass
  rejecting duplicate keys at any depth, mapping-root and string-key checks),
  `ConfigError` naming the offending file, `LoadedConfig[ModelT]` carrying the
  validated model, raw mapping, and a canonical `config_hash` over the
  validated dump (defaults included) for run manifests.
- `src/leanfaith/config/paths.py`: repo-root discovery plus `RepoPaths`
  accessors for the declared §7 top-level directories; no machine-local
  absolute paths are hard-coded anywhere.
- `src/leanfaith/config/logging.py`: single stderr handler under the
  `leanfaith` namespace.
- Dependencies: `pyyaml>=6.0` (runtime), `types-pyyaml` (dev).

## Acceptance evidence

```text
uv run ruff check .          → All checks passed!
uv run ruff format --check . → 13 files already formatted
uv run mypy                  → Success: no issues found in 9 source files
uv run pytest                → 39 passed
```

Failure paths tested: unknown top-level/nested key, duplicate key (top-level
and nested), non-mapping root, invalid YAML, missing file, missing/empty
secret, invalid secret env-name pattern, non-canonical hash inputs (NaN/Inf,
datetime, Path, bytes, non-string keys), frozen-model mutation.

## Notes / deviations

- None. `config_hash` semantics (validated dump, defaults included) chosen so
  a changed default is a changed config; recorded here as the binding rule.

**Next:** LF-003 — IDs/manifests (canonical JSON IDs, run/output manifests,
migration map).

# LF-003 — IDs and manifests

**Date:** 2026-07-10
**Scope (PLAN.md §26):** canonical JSON, content hashes, run/output manifests, migration map.

## Delivered

- `src/leanfaith/schemas/ids.py`: `make_id(prefix, payload)` =
  `prefix:sha256(canonical_json(payload))` (§11.12). Enforced: lowercase
  prefix grammar; canonical-JSON payloads only (datetimes/Path/NaN rejected via
  LF-002 canonicalization); an explicit machine-local-path guard rejecting
  strings under `$HOME`, the current working directory, and `/tmp` so semantic
  IDs must use repo-relative paths. Lean code strings (e.g. `/- comment -/`)
  pass. `parse_id` / `is_valid_id` / `id_prefix` helpers.
- `src/leanfaith/schemas/enums.py`: operational enums `ArtifactClass`
  (production/smoke/diagnostic, §5.1/§22.7) and `DataStage` (§10 lifecycle).
  §11.1 semantic enums land with LF-004.
- `src/leanfaith/schemas/manifest.py`:
  - `RunManifest` per §28.2: run/artifact class, command/argv, `CodeState`
    (exact git revision + dirty flag, fail-closed outside git), environment &
    `environment_schema_version`, config/input/output hashes, seeds, execution
    settings, source/provider/prompt revisions, status counts, retries,
    measurements (tokens/calls/cost/elapsed), tracker ID/offline path,
    parent/resume pointers, UTC-enforced `created_at`.
  - `OutputManifest` per §10: stage, source/revision, config hash, record
    schema version, row count, shard IDs, sha256 file checksums, input
    manifest hashes, code state.
  - `MigrationMap`: explicit old→new ID mapping manifest (§11.12).
  - `write_manifest` (canonical JSON + newline, returns file sha256),
    `read_manifest` (fail-closed on unknown keys/bad JSON), `manifest_hash`,
    `run_manifest_path` (`runs/<run_id>/manifest.json`), `new_run_id`
    (`run_<UTCstamp>_<8hex>`; run IDs are operational, not semantic).

## Acceptance evidence

```text
uv run ruff check .          → All checks passed!
uv run ruff format --check . → all files formatted
uv run mypy                  → Success: no issues found in 13 source files
uv run pytest                → 79 passed
```

Failure paths tested: invalid prefix, timestamp/Path payloads, machine-local
paths (home/cwd//tmp), malformed IDs; naive timestamps, unknown manifest keys,
missing/corrupt manifest files, bad run-id/nonce/checksum/row-count, code
state outside a git checkout.

## Notes / deviations

- Machine-local path rejection is a mechanical guard (home/cwd//tmp roots);
  the full discipline remains a caller obligation per §11.12. Recorded as the
  binding heuristic.

**Next:** LF-004 — canonical record schemas (§11 modules with cross-record
invariants).

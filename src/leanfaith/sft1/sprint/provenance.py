"""Sidecar-derived provenance for compacted sprint views.

A resumed run may span several implementation segments: different engine
source hashes (hence compile contexts), different runner commits, and
different semantic-cache key schemas.  Provenance is therefore derived from
the sidecars themselves and validated for consistency, never copied from the
run manifest written at the first launch.  Cache-key schemas are recovered by
recomputing both known key layouts against the cache's root records.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from leanfaith.config.hashing import hash_canonical, sha256_hex
from leanfaith.sft1.sprint.store import SemanticCache, read_json_object

ENGINE_RELATIVE_PATH = "LeanFaith/Meta/SFT1/Sprint.lean"
CACHE_SCHEMA_LEGACY = 1
CACHE_SCHEMA_CURRENT = 2


def legacy_root_key(
    *,
    project_revision: str,
    lean_version: str,
    import_options_fingerprint: str,
    engine_semantic_version: str,
    name: str,
) -> str:
    """Schema-1 root key layout (no ``cache_schema`` field); validation only."""

    return hash_canonical(
        {
            "kind": "sprint_root",
            "project_revision": project_revision,
            "lean_version": lean_version,
            "import_options_fingerprint": import_options_fingerprint,
            "engine_semantic_version": engine_semantic_version,
            "name": name,
        }
    )


def legacy_op_key(
    *,
    reference_alpha_hash: str,
    operation_id: str,
    engine_semantic_version: str,
    lean_version: str,
    project_revision: str,
    import_options_fingerprint: str,
) -> str:
    """Schema-1 operation key layout (no root name); validation only."""

    return hash_canonical(
        {
            "kind": "sprint_operation",
            "reference_alpha_hash": reference_alpha_hash,
            "operation_id": operation_id,
            "engine_semantic_version": engine_semantic_version,
            "lean_version": lean_version,
            "project_revision": project_revision,
            "import_options_fingerprint": import_options_fingerprint,
        }
    )


def engine_commit_map(repo_root: Path) -> dict[str, list[str]]:
    """Map every historical engine source SHA-256 to the commits carrying it."""

    commits = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "log",
            "--format=%H",
            "--reverse",
            "--",
            ENGINE_RELATIVE_PATH,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    mapping: dict[str, list[str]] = {}
    for commit in commits:
        blob = subprocess.run(
            ["git", "-C", str(repo_root), "show", f"{commit}:{ENGINE_RELATIVE_PATH}"],
            check=True,
            capture_output=True,
        ).stdout
        mapping.setdefault(sha256_hex(blob), []).append(commit)
    return mapping


class CacheSchemaResolver:
    """Recover the cache-key schema used for a sidecar's operation record."""

    def __init__(self, cache_root: Path) -> None:
        self.cache = SemanticCache(cache_root)
        self._alpha: dict[tuple[str, ...], str | None] = {}

    def reference_alpha_hash(self, sidecar: Mapping[str, Any]) -> str | None:
        engine = sidecar["engine"]
        project = sidecar["project"]
        name = str(sidecar["root_name"])
        key = (
            str(project["project_revision"]),
            str(project["lean_version"]),
            str(engine["import_options_fingerprint"]),
            str(engine["semantic_version"]),
            name,
        )
        if key in self._alpha:
            return self._alpha[key]
        common = {
            "project_revision": key[0],
            "lean_version": key[1],
            "import_options_fingerprint": key[2],
            "engine_semantic_version": key[3],
            "name": name,
        }
        result: str | None = None
        for root_key in (SemanticCache.root_key(**common), legacy_root_key(**common)):
            record = self.cache.get_root(root_key)
            if record is not None and isinstance(record.get("reference_alpha_hash"), str):
                result = str(record["reference_alpha_hash"])
                break
        self._alpha[key] = result
        return result

    def schema(self, sidecar: Mapping[str, Any]) -> int | None:
        alpha = self.reference_alpha_hash(sidecar)
        if alpha is None:
            return None
        engine = sidecar["engine"]
        project = sidecar["project"]
        common = {
            "reference_alpha_hash": alpha,
            "operation_id": str(sidecar["operation_id"]),
            "engine_semantic_version": str(engine["semantic_version"]),
            "lean_version": str(project["lean_version"]),
            "project_revision": str(project["project_revision"]),
            "import_options_fingerprint": str(engine["import_options_fingerprint"]),
        }
        cache_key = str(sidecar.get("cache_key", ""))
        if SemanticCache.op_key(**common, name=str(sidecar["root_name"])) == cache_key:
            return CACHE_SCHEMA_CURRENT
        if legacy_op_key(**common) == cache_key:
            return CACHE_SCHEMA_LEGACY
        return None


SQUARE_ENGINE_FIELDS = (
    "source_sha256",
    "compile_context_id",
    "semantic_version",
    "import_options_fingerprint",
)


def _generating_run_verifies(
    runs_root: Path | None, run_name: str, root_name: str, commit: object
) -> str | None:
    """Check that ``runs/<run_name>`` recorded ``commit`` and processed ``root_name`` via Lean."""
    if runs_root is None:
        return "generating run manifest unavailable"
    manifest_path = runs_root / run_name / "run.json"
    journal_path = runs_root / run_name / "journal.jsonl"
    if not manifest_path.is_file() or not journal_path.is_file():
        return f"generating run {run_name} manifest or journal absent"
    if read_json_object(manifest_path).get("implementation_commit") != commit:
        return f"generating run {run_name} recorded a different implementation commit"
    with journal_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if '"square_terminal"' not in line:
                continue
            record = json.loads(line)
            if (
                record.get("kind") == "square_terminal"
                and record.get("root") == root_name
                and record.get("source") == "lean"
            ):
                return None
    return f"generating run {run_name} has no Lean terminal for the root"


class SnapshotStore:
    """Content-addressed cache-record snapshots packed inside a release directory."""

    def __init__(self, release_dir: Path | None) -> None:
        self.release_dir = release_dir
        self._files: dict[str, list[str]] = {}

    def load(self, snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
        if self.release_dir is None:
            return None
        name = str(snapshot.get("file", ""))
        line = snapshot.get("line")
        if not name or not isinstance(line, int):
            return None
        if name not in self._files:
            path = self.release_dir / name
            if not path.is_file():
                return None
            self._files[name] = path.read_text(encoding="utf-8").splitlines()
        lines = self._files[name]
        if line < 0 or line >= len(lines):
            return None
        loaded = json.loads(lines[line])
        return dict(loaded) if isinstance(loaded, dict) else None


def _square_key_identity(sidecar: Mapping[str, Any], block: Mapping[str, Any]) -> str:
    """Recompute the cache key from sidecar fields for schema 2 (legacy) or 3."""
    engine = sidecar.get("engine") or {}
    project = sidecar.get("project") or {}
    schema = block.get("schema")
    revision = block.get("revision", 0)
    identity: dict[str, Any] = {
        "kind": "square_root",
        "cache_schema": schema,
        "operation_id": str(sidecar.get("operation_id")),
        "name": str(sidecar.get("root_name")),
        "engine_semantic_version": str(engine.get("semantic_version")),
        "project_revision": str(project.get("project_revision")),
        "lean_version": str(project.get("lean_version")),
        "import_options_fingerprint": str(engine.get("import_options_fingerprint")),
    }
    if schema == 3:
        identity["operation_revision"] = int(revision) if isinstance(revision, int) else 0
        identity["engine_source_sha256"] = str(engine.get("source_sha256"))
        identity["compile_context_id"] = str(engine.get("compile_context_id"))
    elif isinstance(revision, int) and revision > 0:
        identity["operation_revision"] = revision
    return hash_canonical(identity)


def _recovered_record_verifies(
    runs_root: Path | None, run_name: str, sidecar: Mapping[str, Any]
) -> str | None:
    """A record recovered from run evidence must match that run's own retained rows."""
    if runs_root is None:
        return "run evidence unavailable"
    retained = runs_root / run_name / "retained.jsonl"
    if not retained.is_file():
        return f"run {run_name} retained file absent"
    hashes = sidecar.get("lean_request_hashes") or {}
    root_name = str(sidecar.get("root_name"))
    with retained.open("r", encoding="utf-8") as handle:
        for line in handle:
            if root_name not in line:
                continue
            item = json.loads(line)
            stored = item["sidecar"]
            if stored.get("root_name") != root_name:
                continue
            if (stored.get("lean_request_hashes") or {}) == hashes:
                return None
            return f"run {run_name} evidence carries different request hashes for the root"
    return f"run {run_name} has no retained rows for the root"


def verify_square_cache(
    sidecar: Mapping[str, Any],
    cache_root: Path,
    runs_root: Path | None = None,
    snapshots: SnapshotStore | None = None,
) -> tuple[int | None, list[str], bool | None]:
    """Verify the cache record a sidecar's rows were built from.

    Release evidence is the content-addressed snapshot packed inside the release (when
    present); the record must hash to the sidecar's ``content_sha256`` and agree with the
    sidecar on root, engine, compile context, terminal status, request hashes, alpha
    hashes, and commit provenance. The live shared cache is compared only for information
    (third return value), so a later cache write can never invalidate a release. Releases
    without snapshots (legacy) are verified against the live record.
    """
    issues: list[str] = []
    block = sidecar.get("cache")
    if not isinstance(block, Mapping):
        return None, ["cache block missing"], None
    key = str(block.get("key", ""))
    schema = block.get("schema")
    engine = sidecar.get("engine") or {}
    if key != _square_key_identity(sidecar, block):
        issues.append("cache key does not match the square-root identity")
    if str(block.get("path", "")) != f"roots/{key[:2]}/{key}.json":
        issues.append("cache path does not match the cache key")
    live_path = cache_root / str(block.get("path", ""))
    live = read_json_object(live_path) if live_path.is_file() else None
    content_sha = block.get("content_sha256")
    snapshot_ref = block.get("snapshot")
    record: dict[str, Any] | None = None
    live_agrees: bool | None = None
    if isinstance(snapshot_ref, Mapping) and snapshots is not None:
        record = snapshots.load(snapshot_ref)
        if record is None:
            issues.append("cache snapshot absent from the release")
        elif hash_canonical(record) != content_sha:
            issues.append("cache snapshot content hash differs from the sidecar")
            record = None
        if live is not None and content_sha is not None:
            live_agrees = hash_canonical(live) == content_sha
    if record is None and not issues:
        if live is None:
            issues.append("cache record absent")
        else:
            record = live
            if content_sha is not None and hash_canonical(live) != content_sha:
                issues.append("live cache record content differs from the sidecar")
                record = None
    if record is None:
        return (int(schema) if isinstance(schema, int) else None), issues, live_agrees
    if record.get("root") != sidecar.get("root_name"):
        issues.append("cache record root differs")
    if record.get("status") != "retained":
        issues.append(f"cache record status {record.get('status')!r} is not retained")
    if record.get("operation_id") != sidecar.get("operation_id"):
        issues.append("cache record operation differs")
    record_engine = record.get("engine") or {}
    for field in SQUARE_ENGINE_FIELDS:
        if record_engine.get(field) != engine.get(field):
            issues.append(f"cache record engine {field} differs")
    hashes = sidecar.get("lean_request_hashes") or {}
    if record.get("process_request_hash") != hashes.get("process"):
        issues.append("cache record process request hash differs")
    if (record.get("render") or {}).get("request_hash") != hashes.get("render"):
        issues.append("cache record render request hash differs")
    if dict(record.get("alpha") or {}) != dict((sidecar.get("square") or {}).get("alpha") or {}):
        issues.append("cache record alpha hashes differ")
    source = str(sidecar.get("implementation_commit_source") or "cache_record")
    if source == "cache_record":
        if not record.get("implementation_commit"):
            issues.append("cache record lacks an implementation commit")
        elif record.get("implementation_commit") != sidecar.get("implementation_commit"):
            issues.append("cache record implementation commit differs")
    elif source.startswith("generating_run_manifest:"):
        if record.get("implementation_commit") not in (None, sidecar.get("implementation_commit")):
            issues.append("cache record implementation commit differs")
        problem = _generating_run_verifies(
            runs_root,
            source.split(":", 1)[1],
            str(sidecar.get("root_name")),
            sidecar.get("implementation_commit"),
        )
        if problem:
            issues.append(problem)
    elif source.startswith("recovered_from_run_evidence:"):
        problem = _recovered_record_verifies(runs_root, source.split(":", 1)[1], sidecar)
        if problem:
            issues.append(problem)
    else:
        issues.append("unknown implementation commit source")
    return (int(schema) if isinstance(schema, int) else None), issues, live_agrees


def segment_key(sidecar: Mapping[str, Any], cache_schema: int | None) -> tuple[str, ...]:
    engine = sidecar["engine"]
    return (
        str(engine["source_sha256"]),
        str(engine["compile_context_id"]),
        str(engine["semantic_version"]),
        str(engine["import_options_fingerprint"]),
        "unresolved" if cache_schema is None else str(cache_schema),
        str(sidecar.get("implementation_commit") or "unrecorded"),
        str(sidecar.get("runner_source_sha256") or "unrecorded"),
    )


def derive_provenance(
    records: Sequence[Mapping[str, Any]],
    *,
    repo_root: Path,
    cache_root: Path,
    release_dir: Path | None = None,
    allow_multiple_project_pins: bool = False,
) -> dict[str, Any]:
    """Segments, identities, and consistency checks derived from sidecars."""

    resolver = CacheSchemaResolver(cache_root)
    snapshots = SnapshotStore(release_dir)
    snapshot_verified = 0
    live_agreeing = 0
    live_disagreeing = 0
    commit_map = engine_commit_map(repo_root)
    segments: dict[tuple[str, ...], dict[str, Any]] = {}
    semantic_versions: set[str] = set()
    repr_identities: set[str] = set()
    project_pins: set[str] = set()
    spec_hashes: set[str] = set()
    square_verified = 0
    cache_issues: dict[str, str] = {}
    for record in records:
        sidecar = record["sidecar"]
        cache_block = sidecar.get("cache")
        if isinstance(cache_block, Mapping) and cache_block.get("kind") == "square_root":
            schema, record_issues, live_agrees = verify_square_cache(
                sidecar, cache_root, runs_root=cache_root.parent / "runs", snapshots=snapshots
            )
            if isinstance(cache_block.get("snapshot"), Mapping) and not record_issues:
                snapshot_verified += 1
            if live_agrees is True:
                live_agreeing += 1
            elif live_agrees is False:
                live_disagreeing += 1
            if record_issues:
                schema = None
                cache_issues.setdefault(str(sidecar.get("root_name")), "; ".join(record_issues))
            else:
                square_verified += 1
        else:
            schema = resolver.schema(sidecar)
        key = segment_key(sidecar, schema)
        segment = segments.setdefault(
            key,
            {
                "engine_source_sha256": key[0],
                "compile_context_id": key[1],
                "engine_semantic_version": key[2],
                "import_options_fingerprint": key[3],
                "cache_schema": None if key[4] == "unresolved" else int(key[4]),
                "implementation_commit": None if key[5] == "unrecorded" else key[5],
                "runner_source_sha256": None if key[6] == "unrecorded" else key[6],
                "engine_commits": commit_map.get(key[0], []),
                "rows": 0,
                "roots": set(),
                "operations": {},
            },
        )
        segment["rows"] += 1
        segment["roots"].add(str(sidecar["root_name"]))
        operation = str(sidecar["operation_id"])
        segment["operations"][operation] = segment["operations"].get(operation, 0) + 1
        semantic_versions.add(str(sidecar["engine"]["semantic_version"]))
        repr_block = sidecar["repr"]
        for endpoint in ("reference", "candidate"):
            repr_identities.add(hash_canonical(repr_block[endpoint]["implementation_identity"]))
            spec_hashes.add(str(repr_block[endpoint]["spec_hash"]))
        project_pins.add(hash_canonical(sidecar["project"]))
    segment_list = []
    for key in sorted(segments):
        segment = dict(segments[key])
        segment["roots"] = len(segment["roots"])
        segment["operations"] = dict(sorted(segment["operations"].items()))
        segment_list.append(segment)
    issues: list[str] = []
    for root_name, text in sorted(cache_issues.items())[:50]:
        issues.append(f"square cache record for {root_name}: {text}")
    if len(cache_issues) > 50:
        issues.append(f"{len(cache_issues) - 50} more square cache record inconsistencies")
    if len(semantic_versions) != 1:
        issues.append(f"multiple engine semantic versions: {sorted(semantic_versions)}")
    if len(repr_identities) != 1:
        issues.append("multiple frozen REPR implementation identities")
    if len(spec_hashes) != 1:
        issues.append("multiple REPR spec hashes")
    if len(project_pins) != 1 and not allow_multiple_project_pins:
        issues.append("multiple project pin sets")
    for segment in segment_list:
        if not segment["engine_commits"]:
            issues.append(
                f"engine source {segment['engine_source_sha256'][:12]} is not in the branch history"
            )
        if segment["cache_schema"] is None:
            issues.append(
                f"cache schema unresolved for engine {segment['engine_source_sha256'][:12]}"
            )
    return {
        "schema_version": 1,
        "derived_from": "sidecars",
        "row_count": len(records),
        "engine_semantic_versions": sorted(semantic_versions),
        "engine_source_sha256_set": sorted({s["engine_source_sha256"] for s in segment_list}),
        "compile_context_ids": sorted({s["compile_context_id"] for s in segment_list}),
        "cache_schemas": sorted(
            {s["cache_schema"] for s in segment_list if s["cache_schema"] is not None}
        ),
        "repr_implementation_identity_count": len(repr_identities),
        "project_pin_set_count": len(project_pins),
        "multiple_project_pins_allowed": allow_multiple_project_pins,
        "square_cache_records_verified": square_verified,
        "square_cache_records_inconsistent": len(cache_issues),
        "square_cache_snapshots_verified": snapshot_verified,
        "square_live_cache_agreeing": live_agreeing,
        "square_live_cache_disagreeing": live_disagreeing,
        "segments": segment_list,
        "consistent": not issues,
        "issues": issues,
    }

"""Sidecar-derived provenance for compacted sprint views.

A resumed run may span several implementation segments: different engine
source hashes (hence compile contexts), different runner commits, and
different semantic-cache key schemas.  Provenance is therefore derived from
the sidecars themselves and validated for consistency, never copied from the
run manifest written at the first launch.  Cache-key schemas are recovered by
recomputing both known key layouts against the cache's root records.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from leanfaith.config.hashing import hash_canonical, sha256_hex
from leanfaith.sft1.sprint.store import SemanticCache

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
) -> dict[str, Any]:
    """Segments, identities, and consistency checks derived from sidecars."""

    resolver = CacheSchemaResolver(cache_root)
    commit_map = engine_commit_map(repo_root)
    segments: dict[tuple[str, ...], dict[str, Any]] = {}
    semantic_versions: set[str] = set()
    repr_identities: set[str] = set()
    project_pins: set[str] = set()
    spec_hashes: set[str] = set()
    for record in records:
        sidecar = record["sidecar"]
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
    if len(semantic_versions) != 1:
        issues.append(f"multiple engine semantic versions: {sorted(semantic_versions)}")
    if len(repr_identities) != 1:
        issues.append("multiple frozen REPR implementation identities")
    if len(spec_hashes) != 1:
        issues.append("multiple REPR spec hashes")
    if len(project_pins) != 1:
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
        "segments": segment_list,
        "consistent": not issues,
        "issues": issues,
    }

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
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from leanfaith.config.hashing import hash_canonical, sha256_hex
from leanfaith.sft1.sprint.store import SemanticCache, read_json_object

ENGINE_RELATIVE_PATH = "LeanFaith/Meta/SFT1/Sprint.lean"
CACHE_SCHEMA_LEGACY = 1
CACHE_SCHEMA_NAMED_ROOT = 2
CACHE_SCHEMA_CURRENT = 3


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
            str(engine.get("source_sha256", "")),
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
        current_key = SemanticCache.root_key(**common, engine_source_sha256=key[4] or None)
        named_key = SemanticCache.root_key(**common)
        for root_key in (current_key, named_key, legacy_root_key(**common)):
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
        engine_source_sha256 = str(engine.get("source_sha256", ""))
        if engine_source_sha256 and (
            SemanticCache.op_key(
                **common,
                name=str(sidecar["root_name"]),
                engine_source_sha256=engine_source_sha256,
            )
            == cache_key
        ):
            return CACHE_SCHEMA_CURRENT
        if SemanticCache.op_key(**common, name=str(sidecar["root_name"])) == cache_key:
            return CACHE_SCHEMA_NAMED_ROOT
        if legacy_op_key(**common) == cache_key:
            return CACHE_SCHEMA_LEGACY
        return None


SQUARE_ENGINE_FIELDS = (
    "source_sha256",
    "compile_context_id",
    "semantic_version",
    "import_options_fingerprint",
)
WAVE4_CACHE_KIND = "wave4_orbit_root"
WAVE4_CACHE_SCHEMA = 2
WAVE4_NEGATIVE_OPERATION = {
    "ORBIT_WAVE4_N31_V1": "N31_DROP_REQUIRED_GUARD_PROOF_V1",
    "ORBIT_WAVE4_N26_V1": "N26_INCREMENT_BOUND_PROOF_V1",
    "ORBIT_WAVE4_N32_V1": "N32_SWAP_ROLE_ORDER_PROOF_V1",
    "ORBIT_WAVE4_N30_V1": "N30_ADD_UNJUSTIFIED_UNIQUENESS_PROOF_V1",
    "ORBIT_WAVE4_N29_V1": "N29_SWAP_WITNESS_DEPENDENCY_PROOF_V1",
    "ORBIT_WAVE4_N25_V1": "N25_TOGGLE_EQ_NE_PROOF_V1",
}
WAVE4_ROW_ENDPOINTS = {
    "preserving_reference": ("p_prime", "p", "p_composite_iff"),
    "preserving_candidate": ("c", "c_prime", "c_composite_iff"),
    "negative_base": ("c", "p", "not_iff_c_p"),
    "negative_last": ("p_prime", "c_prime", "not_iff_p_prime_c_prime"),
}
_SHA256 = re.compile(r"[0-9a-f]{64}")


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
        self._records: dict[tuple[str, int], dict[str, Any] | None] = {}

    def load(self, snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
        if self.release_dir is None:
            return None
        name = str(snapshot.get("file", ""))
        line = snapshot.get("line")
        if not name or type(line) is not int:
            return None
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            return None
        cache_key = (name, line)
        if cache_key in self._records:
            record = self._records[cache_key]
            return None if record is None else dict(record)
        if name not in self._files:
            release_root = self.release_dir.resolve()
            path = (release_root / relative).resolve()
            if not path.is_relative_to(release_root) or not path.is_file():
                return None
            try:
                self._files[name] = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                self._records[cache_key] = None
                return None
        lines = self._files[name]
        if line < 0 or line >= len(lines):
            return None
        try:
            loaded = json.loads(lines[line])
        except (json.JSONDecodeError, UnicodeDecodeError):
            loaded = None
        record = dict(loaded) if isinstance(loaded, dict) else None
        self._records[cache_key] = record
        return None if record is None else dict(record)


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


def _required_mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{field} must be a string-keyed mapping")
    return cast(Mapping[str, Any], value)


def _required_sequence(value: object, field: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise ValueError(f"{field} must be a sequence")
    return value


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be nonempty text")
    return value


def _required_sha256(value: object, field: str) -> str:
    text = _required_text(value, field)
    if _SHA256.fullmatch(text) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return text


def _wave4_project_identity(
    sidecar: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Validate the project/import/options identity carried by a Wave 4 row."""

    project = _required_mapping(sidecar.get("project"), "wave4 sidecar project")
    engine = _required_mapping(sidecar.get("engine"), "wave4 sidecar engine")
    for field in (
        "project_id",
        "project_dir",
        "project_revision",
        "lean_version",
        "lean_interact_version",
        "repl_revision",
        "import_header",
    ):
        _required_text(project.get(field), f"wave4 sidecar project.{field}")
    options = _required_mapping(project.get("options"), "wave4 sidecar project.options")
    if any(not isinstance(value, str | int | float | bool) for value in options.values()):
        raise ValueError("wave4 sidecar project.options contains an unsupported value")
    expected_import_options = hash_canonical(
        {
            "import_header": project["import_header"],
            "options": dict(sorted(options.items())),
            "lean_version": project["lean_version"],
            "project_revision": project["project_revision"],
        }
    )
    if engine.get("import_options_fingerprint") != expected_import_options:
        raise ValueError("wave4 engine import/options fingerprint differs from the project pins")
    _required_sha256(engine.get("source_sha256"), "wave4 sidecar engine.source_sha256")
    _required_text(engine.get("semantic_version"), "wave4 sidecar engine.semantic_version")
    context_id = _required_text(
        engine.get("compile_context_id"), "wave4 sidecar engine.compile_context_id"
    )
    if not context_id.startswith("ctx:"):
        raise ValueError("wave4 engine compile context is not a canonical ctx identity")
    return project, engine


def _wave4_render_verifies(
    selected: Sequence[Mapping[str, Any]],
    *,
    sidecar: Mapping[str, Any],
    engine: Mapping[str, Any],
) -> None:
    """Bind every selected frozen render to its source statement and context."""

    statement = _required_text(sidecar.get("statement"), "wave4 sidecar statement")
    context_id = engine["compile_context_id"]
    for item_index, item in enumerate(selected):
        render = _required_mapping(item.get("render"), f"wave4 selected[{item_index}].render")
        for endpoint in ("p", "c", "p_prime", "c_prime"):
            block = _required_mapping(
                render.get(endpoint), f"wave4 selected[{item_index}].render.{endpoint}"
            )
            record = _required_mapping(
                block.get("record"), f"wave4 selected[{item_index}].render.{endpoint}.record"
            )
            material = _required_mapping(
                block.get("source_material"),
                f"wave4 selected[{item_index}].render.{endpoint}.source_material",
            )
            if record.get("compile_context_id") != context_id:
                raise ValueError("wave4 rendered endpoint changes the compile context")
            if record.get("source_material_hash") != hash_canonical(material):
                raise ValueError("wave4 rendered endpoint source-material hash differs")
            if endpoint == "p":
                if (
                    material.get("kind") != "raw_statement"
                    or material.get("raw_statement") != statement
                ):
                    raise ValueError("wave4 source endpoint does not bind the root statement")
            elif material.get("kind") != "constructed_expr_no_source_text":
                raise ValueError("wave4 constructed endpoint claims source text")


def _wave4_record_verifies(
    sidecar: Mapping[str, Any], record: Mapping[str, Any], *, policy: Any
) -> None:
    """Replay one Wave 4 snapshot's exact computation and row-local bindings."""

    # Local imports avoid a module cycle: square owns the executable validator and imports
    # this module only for release-time provenance derivation.
    from leanfaith.sft1.sprint.square import (
        Wave4Runner,
        preselect_wave4_variant_descriptors,
        select_wave4_variants,
        validate_wave4_root_payload,
        wave4_cache_key,
    )

    project, engine = _wave4_project_identity(sidecar)
    root = _required_text(sidecar.get("root_name"), "wave4 sidecar root_name")
    operation = _required_text(sidecar.get("operation_id"), "wave4 sidecar operation_id")
    negative = WAVE4_NEGATIVE_OPERATION.get(operation)
    if negative is None or sidecar.get("negative_operation") != negative:
        raise ValueError("wave4 sidecar operation/negative identity is unknown or inconsistent")
    expected_root_id = "root:" + hash_canonical(
        [project["project_id"], project["project_revision"], root]
    )
    if sidecar.get("root_id") != expected_root_id:
        raise ValueError("wave4 ancestry root identity differs from its project-qualified source")

    if record.get("schema_version") != 1 or record.get("kind") != WAVE4_CACHE_KIND:
        raise ValueError("wave4 cache record schema/kind differs")
    if record.get("cache_schema") != WAVE4_CACHE_SCHEMA:
        raise ValueError("wave4 cache record uses an ambiguous legacy schema")
    if record.get("root") != root or record.get("operation_id") != operation:
        raise ValueError("wave4 cache record root/operation differs")
    if record.get("status") != "retained":
        raise ValueError("wave4 cache record is not retained")
    if dict(_required_mapping(record.get("engine"), "wave4 cache engine")) != dict(engine):
        raise ValueError("wave4 cache record engine identity differs")
    if not record.get("implementation_commit") or record.get(
        "implementation_commit"
    ) != sidecar.get("implementation_commit"):
        raise ValueError("wave4 cache record implementation commit differs")

    policy_hash = _required_sha256(record.get("policy_hash"), "wave4 cache policy_hash")
    if policy_hash != policy.policy_hash:
        raise ValueError("wave4 cache checker/policy identity differs from the pinned policy")
    maximum_depth = record.get("maximum_depth")
    if type(maximum_depth) is not int or not 1 <= maximum_depth <= 3:
        raise ValueError("wave4 cache maximum depth is invalid")
    revision = record.get("operation_revision")
    if type(revision) is not int or revision < 0:
        raise ValueError("wave4 cache operation revision is invalid")

    cache = _required_mapping(sidecar.get("cache"), "wave4 sidecar cache")
    if cache.get("revision") != revision:
        raise ValueError("wave4 sidecar/cache operation revision differs")
    expected_key = wave4_cache_key(
        operation_id=operation,
        name=root,
        policy_hash=policy_hash,
        maximum_depth=maximum_depth,
        engine_source_sha256=str(engine["source_sha256"]),
        compile_context_id=str(engine["compile_context_id"]),
        engine_semantic_version=str(engine["semantic_version"]),
        project_revision=str(project["project_revision"]),
        lean_version=str(project["lean_version"]),
        import_options_fingerprint=str(engine["import_options_fingerprint"]),
        revision=revision,
    )
    if cache.get("key") != expected_key:
        raise ValueError("wave4 cache key does not bind its exact computation identity")

    payload = _required_mapping(record.get("payload"), "wave4 cache payload")
    if payload.get("root") != root or payload.get("operation_id") != operation:
        raise ValueError("wave4 payload root/operation differs")
    if payload.get("negative_operation") != negative:
        raise ValueError("wave4 payload negative operation differs")
    if payload.get("engine_semantic_version") != engine["semantic_version"]:
        raise ValueError("wave4 payload checker/engine semantic version differs")
    if payload.get("module") != sidecar.get("module"):
        raise ValueError("wave4 payload source module differs")
    if payload.get("level_params") != sidecar.get("level_params"):
        raise ValueError("wave4 payload source universe parameters differ")

    descriptors = preselect_wave4_variant_descriptors(
        payload,
        operation_id=operation,
        policy=policy,
        maximum_depth=maximum_depth,
        expected_root=root,
        selection_root_id=expected_root_id,
    )
    validated = validate_wave4_root_payload(
        payload,
        operation_id=operation,
        policy=policy,
        maximum_depth=maximum_depth,
        expected_root=root,
        selected_descriptors=descriptors,
        selection_root_id=expected_root_id,
    )
    expected_selected = select_wave4_variants(validated, policy)
    stored_selected = tuple(
        _required_mapping(item, f"wave4 cache selected[{index}]")
        for index, item in enumerate(
            _required_sequence(record.get("selected"), "wave4 cache selected")
        )
    )
    if len(stored_selected) != len(expected_selected):
        raise ValueError("wave4 cache selected variant count differs")
    identity_fields = (
        "index",
        "selection_hash",
        "content_hash",
        "reference_chain_hash",
        "candidate_chain_hash",
        "reference_site_hash",
        "candidate_site_hash",
    )
    for stored, expected in zip(stored_selected, expected_selected, strict=True):
        for field in identity_fields:
            if stored.get(field) != getattr(expected, field):
                raise ValueError(f"wave4 selected {field} differs from the certified descriptor")
        if stored.get("variant") != expected.raw:
            raise ValueError("wave4 selected variant differs from the certified payload")
    if (
        record.get("enumeration_hash") != validated.enumeration_hash
        or payload.get("enumeration_hash") != validated.enumeration_hash
    ):
        raise ValueError("wave4 complete-enumeration identity differs")

    hashes = _required_mapping(sidecar.get("lean_request_hashes"), "wave4 request hashes")
    if record.get("process_request_hash") != hashes.get("process"):
        raise ValueError("wave4 process request hash differs")
    if record.get("render_request_hash") != hashes.get("render"):
        raise ValueError("wave4 render request hash differs")
    _wave4_render_verifies(stored_selected, sidecar=sidecar, engine=engine)

    wave4 = _required_mapping(sidecar.get("wave4"), "wave4 row identity")
    row_kind = _required_text(sidecar.get("row_kind"), "wave4 row kind")
    if row_kind not in WAVE4_ROW_ENDPOINTS or wave4.get("logical_role") != row_kind:
        raise ValueError("wave4 row role is unknown or inconsistent")
    expected_label = row_kind in {"preserving_reference", "preserving_candidate"}
    if sidecar.get("label") is not expected_label:
        raise ValueError("wave4 row label differs from its certified role")
    if row_kind == "negative_base":
        selected_item = stored_selected[0]
        if wave4.get("negative_operation") != negative:
            raise ValueError("wave4 base row changes the negative operation")
    else:
        selection_hash = wave4.get("selection_hash")
        matches = [item for item in stored_selected if item.get("selection_hash") == selection_hash]
        if len(matches) != 1:
            raise ValueError("wave4 row does not select exactly one cached variant")
        selected_item = matches[0]
        for field in identity_fields[1:]:
            if wave4.get(field) != selected_item.get(field):
                raise ValueError(f"wave4 row {field} differs from the cache selection")
        if wave4.get("enumeration_hash") != validated.enumeration_hash:
            raise ValueError("wave4 row enumeration hash differs")

    variant = _required_mapping(selected_item.get("variant"), "wave4 selected variant")
    if row_kind != "negative_base" and (
        wave4.get("variant_index") != selected_item.get("index")
        or wave4.get("depth") != variant.get("depth")
    ):
        raise ValueError("wave4 row variant index/depth differs from the cache selection")
    evidence = _required_mapping(variant.get("evidence"), "wave4 selected evidence")
    selection_hash = str(selected_item["selection_hash"])
    expected_evidence, expected_check = Wave4Runner._row_evidence(
        row_kind, evidence, selection_hash
    )
    if sidecar.get("evidence") != expected_evidence:
        raise ValueError("wave4 row evidence differs from its certified closure")
    if sidecar.get("evidence_hash") != hash_canonical(expected_evidence):
        raise ValueError("wave4 row evidence hash differs")
    if sidecar.get("row_check") != expected_check:
        raise ValueError("wave4 row checker evidence differs")
    base_hash = hash_canonical(evidence.get("base_candidate_refutation"))
    if row_kind == "negative_base" and wave4.get("base_negative_evidence_hash") != base_hash:
        raise ValueError("wave4 base row negative-family evidence hash differs")

    if row_kind == "negative_base":
        expected_site_hash = hash_canonical(
            {
                "direction": evidence.get("direction"),
                "base_candidate_refutation": evidence.get("base_candidate_refutation"),
            }
        )
    elif row_kind == "preserving_reference":
        expected_site_hash = str(selected_item["reference_site_hash"])
    elif row_kind == "preserving_candidate":
        expected_site_hash = str(selected_item["candidate_site_hash"])
    else:
        expected_site_hash = hash_canonical(
            [selected_item["reference_site_hash"], selected_item["candidate_site_hash"]]
        )
    if sidecar.get("site") != {"kind": "wave4_chain", "detail": expected_site_hash}:
        raise ValueError("wave4 row selected site identity differs from the cache selection")

    reference_endpoint, candidate_endpoint, _check = WAVE4_ROW_ENDPOINTS[row_kind]
    render = _required_mapping(selected_item.get("render"), "wave4 selected render")
    shared_base = row_kind == "negative_base"
    expected_reference = Wave4Runner._render_endpoint(
        render, reference_endpoint, shared_base=shared_base
    )
    expected_candidate = Wave4Runner._render_endpoint(
        render, candidate_endpoint, shared_base=shared_base
    )
    row_repr = _required_mapping(sidecar.get("repr"), "wave4 row repr")
    if row_repr.get("reference") != expected_reference.get("record") or row_repr.get(
        "reference_source_material"
    ) != expected_reference.get("source_material"):
        raise ValueError("wave4 row reference endpoint differs from the cache snapshot")
    if row_repr.get("candidate") != expected_candidate.get("record") or row_repr.get(
        "candidate_source_material"
    ) != expected_candidate.get("source_material"):
        raise ValueError("wave4 row candidate endpoint differs from the cache snapshot")


def _verify_wave4_cache(
    sidecar: Mapping[str, Any],
    cache_root: Path,
    *,
    snapshots: SnapshotStore | None,
    repo_root: Path | None,
    policy: Any | None,
) -> tuple[int | None, list[str], bool | None]:
    """Verify an immutable Wave 4 root snapshot; live cache is telemetry only."""

    issues: list[str] = []
    block = sidecar.get("cache")
    if not isinstance(block, Mapping):
        return None, ["cache block missing"], None
    schema = block.get("schema")
    if block.get("kind") != WAVE4_CACHE_KIND or schema != WAVE4_CACHE_SCHEMA:
        issues.append("Wave 4 cache kind/schema is unknown or legacy-ambiguous")
    key = str(block.get("key", ""))
    if not key or str(block.get("path", "")) != f"roots/{key[:2]}/{key}.json":
        issues.append("Wave 4 cache path does not match its key")
    content_sha = block.get("content_sha256")
    if not isinstance(content_sha, str) or _SHA256.fullmatch(content_sha) is None:
        issues.append("Wave 4 cache content hash is absent or malformed")

    snapshot_ref = block.get("snapshot")
    if not isinstance(snapshot_ref, Mapping) or snapshots is None:
        issues.append("Wave 4 release requires an immutable cache snapshot")
        return (schema if type(schema) is int else None), issues, None
    if snapshot_ref.get("content_sha256") != content_sha:
        issues.append("Wave 4 snapshot reference content hash differs from the sidecar")
    record = snapshots.load(snapshot_ref)
    if record is None:
        issues.append("Wave 4 cache snapshot is absent or malformed")
        return (schema if type(schema) is int else None), issues, None
    if hash_canonical(record) != content_sha:
        issues.append("Wave 4 cache snapshot content hash differs from the sidecar")
        return (schema if type(schema) is int else None), issues, None

    live_path = cache_root / str(block.get("path", ""))
    try:
        live = read_json_object(live_path) if live_path.is_file() else None
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        live = {}
    live_agrees = None if live is None else hash_canonical(live) == content_sha
    try:
        if repo_root is None:
            raise ValueError("Wave 4 provenance requires the repository root")
        if policy is None:
            from leanfaith.sft1.sprint.square import load_wave4_config

            policy = load_wave4_config(repo_root).policy
        _wave4_record_verifies(sidecar, record, policy=policy)
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        issues.append(f"Wave 4 cache record invalid: {exc}")
    return (schema if type(schema) is int else None), issues, live_agrees


def verify_square_cache(
    sidecar: Mapping[str, Any],
    cache_root: Path,
    runs_root: Path | None = None,
    snapshots: SnapshotStore | None = None,
    *,
    repo_root: Path | None = None,
    wave4_policy: Any | None = None,
) -> tuple[int | None, list[str], bool | None]:
    """Verify the cache record a sidecar's rows were built from.

    Release evidence is the content-addressed snapshot packed inside the release (when
    present); the record must hash to the sidecar's ``content_sha256`` and agree with the
    sidecar on root, engine, compile context, terminal status, request hashes, alpha
    hashes, and commit provenance. The live shared cache is compared only for information
    (third return value), so a later cache write can never invalidate a release. Releases
    without snapshots (legacy) are verified against the live record.
    """
    block = sidecar.get("cache")
    if not isinstance(block, Mapping):
        return None, ["cache block missing"], None
    if block.get("kind") == WAVE4_CACHE_KIND:
        return _verify_wave4_cache(
            sidecar,
            cache_root,
            snapshots=snapshots,
            repo_root=repo_root,
            policy=wave4_policy,
        )
    issues: list[str] = []
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
    square_snapshot_verified = 0
    wave4_snapshot_verified = 0
    live_agreeing = 0
    live_disagreeing = 0
    commit_map = engine_commit_map(repo_root)
    segments: dict[tuple[str, ...], dict[str, Any]] = {}
    semantic_versions: set[str] = set()
    repr_identities: set[str] = set()
    project_pins: set[str] = set()
    spec_hashes: set[str] = set()
    square_verified = 0
    square_inconsistent = 0
    wave4_verified = 0
    wave4_inconsistent = 0
    cache_issues: dict[str, str] = {}
    wave4_policy: Any | None = None
    for record in records:
        sidecar = record["sidecar"]
        cache_block = sidecar.get("cache")
        cache_kind = cache_block.get("kind") if isinstance(cache_block, Mapping) else None
        if cache_kind in {"square_root", WAVE4_CACHE_KIND}:
            if cache_kind == WAVE4_CACHE_KIND and wave4_policy is None:
                from leanfaith.sft1.sprint.square import load_wave4_config

                wave4_policy = load_wave4_config(repo_root).policy
            schema, record_issues, live_agrees = verify_square_cache(
                sidecar,
                cache_root,
                runs_root=cache_root.parent / "runs",
                snapshots=snapshots,
                repo_root=repo_root,
                wave4_policy=wave4_policy,
            )
            if isinstance(cache_block.get("snapshot"), Mapping) and not record_issues:
                if cache_kind == WAVE4_CACHE_KIND:
                    wave4_snapshot_verified += 1
                else:
                    square_snapshot_verified += 1
            if live_agrees is True:
                live_agreeing += 1
            elif live_agrees is False:
                live_disagreeing += 1
            if record_issues:
                schema = None
                cache_issues.setdefault(str(sidecar.get("root_name")), "; ".join(record_issues))
                if cache_kind == WAVE4_CACHE_KIND:
                    wave4_inconsistent += 1
                else:
                    square_inconsistent += 1
            elif cache_kind == WAVE4_CACHE_KIND:
                wave4_verified += 1
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
        issues.append(f"cache record for {root_name}: {text}")
    if len(cache_issues) > 50:
        issues.append(f"{len(cache_issues) - 50} more cache record inconsistencies")
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
        "square_cache_records_inconsistent": square_inconsistent,
        "wave4_cache_records_verified": wave4_verified,
        "wave4_cache_records_inconsistent": wave4_inconsistent,
        "square_cache_snapshots_verified": square_snapshot_verified,
        "wave4_cache_snapshots_verified": wave4_snapshot_verified,
        "square_live_cache_agreeing": live_agreeing,
        "square_live_cache_disagreeing": live_disagreeing,
        "segments": segment_list,
        "consistent": not issues,
        "issues": issues,
    }

"""Lean-free integrity validator for compacted sprint views.

Checks row/sidecar joins, content hashes, label polarity, evidence flags,
render-hash and pair-id recomputation, unordered-pair uniqueness, shard
conservation against the compaction manifest and the retained records, the
run's final status, the replay receipt, and sidecar-derived provenance with
mixed engine identities.  Proof checks happened during original generation;
the replay receipt certifies journal/cache replay of stored terminals, not a
fresh kernel replay, and this validator records that distinction.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.paths import find_repo_root
from leanfaith.schemas.ids import PAIR_PREFIX, make_id
from leanfaith.sft1.sprint.engine import NEGATIVE_OPERATIONS, POSITIVE_OPERATIONS, mechanism_of
from leanfaith.sft1.sprint.provenance import derive_provenance, legacy_op_key, legacy_root_key
from leanfaith.sft1.sprint.runner import release_certificate_issues
from leanfaith.sft1.sprint.screens import residue_violation, unordered_pair_key
from leanfaith.sft1.sprint.store import SemanticCache, read_json_object, write_atomic

ROW_FIELDS = {"pair_id", "root_id", "reference", "candidate", "label", "operation_id"}
MODEL_FACING_ROW_FIELDS = {"reference", "candidate", "label"}
VIEW_SIDECAR_FIELDS = {
    "orientation",
    "core_family",
    "core_cell",
    "row_schema",
    "stored_reference_is",
    "orientation_rule",
    "mechanism",
    "group_id",
    "release",
}

WAVE3_RELEASE_SCHEMA = 3
WAVE3_CACHE_SNAPSHOT_SCHEMA = 1
WAVE3_CACHE_SNAPSHOT_FILE = "source_cache/snapshots.jsonl"
WAVE3_PROJECTS = frozenset({"mathlib", "physlib", "cslib"})
WAVE3_GATE_OPERATIONS = frozenset(
    {
        "N26_INCREMENT_BOUND_PROOF_V1",
        "N29_SWAP_WITNESS_DEPENDENCY_PROOF_V1",
        "N30_ADD_UNJUSTIFIED_UNIQUENESS_PROOF_V1",
        "N31_DROP_REQUIRED_GUARD_PROOF_V1",
        "N32_SWAP_ROLE_ORDER_PROOF_V1",
    }
)
WAVE4_RELEASE_SCHEMA = 4
WAVE4_RELEASE_ID_PREFIX = "wave4_release:"
WAVE4_PROJECTS = frozenset({"mathlib", "physlib", "cslib"})
WAVE4_ROW_LABELS = {
    "preserving_reference": True,
    "preserving_candidate": True,
    "negative_base": False,
    "negative_last": False,
}
_RUN_RECEIPT_FILES = {
    "manifest": "run.json",
    "status": "status.json",
    "journal": "journal.jsonl",
    "retained": "retained.jsonl",
    "replay": "replay_report.json",
}


def _without_view_fields(sidecar: Mapping[str, Any]) -> dict[str, Any]:
    result = {k: v for k, v in sidecar.items() if k not in VIEW_SIDECAR_FIELDS}
    cache = result.get("cache")
    if isinstance(cache, Mapping):
        # Snapshot locations are assigned only during release compaction. They do not
        # change the source record that the compacted sidecar must match.
        result["cache"] = {**cache, "snapshot": None}
    return result


REPLAY_SEMANTICS = (
    "journal_and_cache_replay_of_stored_terminals; proof checks occurred during original "
    "generation; no fresh kernel replay"
)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for line in path.read_bytes().split(b"\n"):
        if line:
            value = json.loads(line.decode("utf-8"))
            if isinstance(value, dict):
                values.append(value)
    return values


SQUARE_ROW_LABELS = {
    "p_prime_iff_p": True,
    "c_iff_c_prime": True,
    "not_iff_c_p": False,
    "not_iff_p_prime_c_prime": False,
}


def is_square_operation(operation: str) -> bool:
    return operation.startswith("SQUARE_")


def expected_label(operation: str, sidecar: Mapping[str, Any]) -> bool | None:
    if is_square_operation(operation):
        return SQUARE_ROW_LABELS.get(str(sidecar.get("row_kind")))
    if operation in POSITIVE_OPERATIONS:
        return True
    if operation in NEGATIVE_OPERATIONS:
        return False
    return None


SQUARE_ROW_TRUTHS: dict[str, tuple[str, str]] = {
    # row kind -> (reference truth, candidate truth), derived from the square endpoints:
    # P and P' are proved (loaded theorem, transported proof), C and C' are refuted.
    "p_prime_iff_p": ("proved", "proved"),
    "c_iff_c_prime": ("refuted", "refuted"),
    "not_iff_c_p": ("refuted", "proved"),
    "not_iff_p_prime_c_prime": ("proved", "refuted"),
}


def _check_square_truths(sidecar: Mapping[str, Any], evidence: Mapping[str, Any]) -> str | None:
    expected = SQUARE_ROW_TRUTHS.get(str(sidecar.get("row_kind")))
    if expected is None:
        return "square_row_kind_unknown"
    reference_truth, candidate_truth = expected
    if (
        evidence.get("reference_truth") != reference_truth
        or evidence.get("candidate_truth") != candidate_truth
    ):
        return "square_truths_not_derived_from_endpoints"
    if (
        sidecar.get("reference_truth") != reference_truth
        or sidecar.get("candidate_truth") != candidate_truth
    ):
        return "square_sidecar_truths_mismatch"
    return None


def _check_evidence(sidecar: Mapping[str, Any], operation: str) -> str | None:
    evidence = sidecar.get("evidence") or {}
    label = expected_label(operation, sidecar)
    if is_square_operation(operation):
        truth_issue = _check_square_truths(sidecar, evidence)
        if truth_issue:
            return truth_issue
        if label is True:
            check = (evidence.get("equivalence_proof") or {}).get("check") or {}
            if not (check.get("meta_checked") and check.get("kernel_checked")):
                return "positive_without_checked_iff_witness"
            return None
        check = (evidence.get("refutation") or {}).get("check") or {}
        source = evidence.get("source_proof_check") or {}
        if (evidence.get("refutation") or {}).get("goal") != "Not (Iff reference candidate)":
            return "negative_without_direct_not_iff_goal"
        if not (check.get("meta_checked") and check.get("kernel_checked")):
            return "negative_without_checked_not_iff"
        if not (source.get("meta_checked") and source.get("kernel_checked")):
            return "negative_without_checked_source_proof"
        return None
    if operation in POSITIVE_OPERATIONS or operation.startswith("P"):
        check = (evidence.get("equivalence_proof") or {}).get("check") or {}
        if not (check.get("meta_checked") and check.get("kernel_checked")):
            return "positive_without_checked_iff_witness"
        if evidence.get("candidate_truth") != "proved_equivalent_to_reference":
            return "positive_candidate_truth_mismatch"
        return None
    refutation = (evidence.get("refutation") or {}).get("check") or {}
    source = evidence.get("source_proof_check") or {}
    if not (refutation.get("meta_checked") and refutation.get("kernel_checked")):
        return "negative_without_checked_refutation"
    if not (source.get("meta_checked") and source.get("kernel_checked")):
        return "negative_without_checked_source_proof"
    if evidence.get("candidate_truth") != "refuted":
        return "negative_candidate_truth_mismatch"
    return None


class AggregateAccumulator:
    """Streaming recomputation of every manifest aggregate from full sidecars."""

    def __init__(self) -> None:
        self.counts: dict[str, dict[str, int]] = {
            "operations": {},
            "mechanisms": {},
            "negative_mechanisms": {},
            "transforms": {},
            "families": {},
            "row_kinds": {},
        }
        self.roots: set[str] = set()
        self.squares: set[str] = set()
        self.rows = 0
        self.positives = 0
        self.curriculum_only = False

    def add(self, sidecar: Mapping[str, Any]) -> None:
        square = sidecar.get("square") or {}
        for name, value in (
            ("operations", sidecar.get("operation_id")),
            ("mechanisms", sidecar.get("mechanism")),
            ("negative_mechanisms", square.get("negative_operation")),
            ("transforms", square.get("t_p")),
            ("families", sidecar.get("core_family")),
            ("row_kinds", sidecar.get("row_kind")),
        ):
            key = str(value)
            self.counts[name][key] = self.counts[name].get(key, 0) + 1
        self.roots.add(str(sidecar.get("root_id")))
        self.squares.add(f"{sidecar.get('root_id')}|{sidecar.get('operation_id')}")
        self.rows += 1
        self.positives += 1 if bool(sidecar.get("label")) else 0
        if str(sidecar.get("operation_id")) == "SQUARE_N19_CURRICULUM_V1":
            self.curriculum_only = True

    def result(self) -> dict[str, Any]:
        return {
            **{name: dict(sorted(values.items())) for name, values in self.counts.items()},
            "roots": len(self.roots),
            "squares": len(self.squares),
            "retained_rows": self.rows,
            "labels": {"positive": self.positives, "negative": self.rows - self.positives},
            "curriculum_only": self.curriculum_only,
        }


def sidecar_aggregate_counts(sidecars: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Recompute every manifest aggregate from the finalized sidecars of a view."""
    accumulator = AggregateAccumulator()
    for sidecar in sidecars:
        accumulator.add(sidecar)
    return accumulator.result()


def manifest_aggregate_issues(
    manifest: Mapping[str, Any],
    sidecars: Sequence[Mapping[str, Any]] = (),
    *,
    derived: Mapping[str, Any] | None = None,
) -> list[str]:
    """Every aggregate the manifest reports must equal the sidecar-derived value.

    Square manifests (those with ``row_kinds``) are checked for all aggregates; other
    manifests only for the aggregates they carry. ``derived`` may be passed from a
    streaming accumulation instead of the sidecars themselves.
    """
    if derived is None:
        if not sidecars:
            return []
        derived = sidecar_aggregate_counts(sidecars)
    elif int(derived.get("retained_rows", 0)) == 0:
        return []
    square_manifest = "row_kinds" in manifest
    issues: list[str] = []
    for name in (
        "operations",
        "mechanisms",
        "negative_mechanisms",
        "transforms",
        "families",
        "row_kinds",
        "roots",
        "squares",
        "retained_rows",
        "labels",
        "curriculum_only",
    ):
        if name not in manifest:
            if square_manifest and name in {
                "operations",
                "mechanisms",
                "negative_mechanisms",
                "families",
                "row_kinds",
                "roots",
                "retained_rows",
                "labels",
            }:
                issues.append(f"{name} missing from the manifest")
            continue
        if manifest[name] != derived[name]:
            issues.append(f"{name}: manifest {manifest[name]!r} != sidecars {derived[name]!r}")
    status = str(manifest.get("artifact_status", ""))
    if square_manifest and status.startswith("square_release") and derived["curriculum_only"]:
        issues.append("curriculum-only view labelled as a core release")
    if square_manifest and status.startswith("curriculum") and not derived["curriculum_only"]:
        issues.append("core view labelled as curriculum-only")
    return issues


PROVENANCE_SIDECAR_FIELDS = (
    "root_name",
    "root_id",
    "operation_id",
    "engine",
    "project",
    "implementation_commit",
    "implementation_commit_source",
    "runner_source_sha256",
    "lean_request_hashes",
    "cache",
    "cache_key",
    "cache_schema",
)


def _slim_for_provenance(sidecar: Mapping[str, Any]) -> dict[str, Any]:
    """Only the sidecar fields provenance derivation and cache verification read."""
    slim: dict[str, Any] = {k: sidecar[k] for k in PROVENANCE_SIDECAR_FIELDS if k in sidecar}
    square = sidecar.get("square") or {}
    slim["square"] = {"alpha": square.get("alpha")}
    repr_block = sidecar.get("repr") or {}
    slim["repr"] = {
        side: {
            "implementation_identity": (repr_block.get(side) or {}).get("implementation_identity"),
            "spec_hash": (repr_block.get(side) or {}).get("spec_hash"),
        }
        for side in ("reference", "candidate")
    }
    return slim


def _pair_id(item: Mapping[str, Any]) -> str:
    """Pair id of a retained record.

    Five-field rows carry it in the row; three-field model-facing rows keep it in the sidecar.
    """
    row = item.get("row") or {}
    if isinstance(row, Mapping) and row.get("pair_id") is not None:
        return str(row["pair_id"])
    sidecar = item["sidecar"]
    assert isinstance(sidecar, Mapping)
    return str(sidecar["pair_id"])


def _sha256_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def wave3_root_cache_key(sidecar: Mapping[str, Any]) -> str:
    """Recompute the regular sprint root-cache key named by a Wave 3 row."""

    engine = sidecar.get("engine")
    project = sidecar.get("project")
    if not isinstance(engine, Mapping) or not isinstance(project, Mapping):
        raise ValueError("Wave 3 sidecar lacks engine/project identity")
    schema = sidecar.get("cache_schema")
    common = {
        "project_revision": str(project.get("project_revision", "")),
        "lean_version": str(project.get("lean_version", "")),
        "import_options_fingerprint": str(engine.get("import_options_fingerprint", "")),
        "engine_semantic_version": str(engine.get("semantic_version", "")),
        "name": str(sidecar.get("root_name", "")),
    }
    if schema == 3:
        source_sha256 = str(engine.get("source_sha256", ""))
        if not _sha256_text(source_sha256):
            raise ValueError("Wave 3 schema-3 cache identity lacks engine source SHA-256")
        return SemanticCache.root_key(**common, engine_source_sha256=source_sha256)
    if schema == 2:
        return SemanticCache.root_key(**common)
    if schema == 1:
        return legacy_root_key(**common)
    raise ValueError(f"Wave 3 cache schema {schema!r} is unsupported")


def wave3_operation_cache_key(sidecar: Mapping[str, Any], *, reference_alpha_hash: str) -> str:
    """Recompute the regular sprint operation-cache key named by a Wave 3 row."""

    engine = sidecar.get("engine")
    project = sidecar.get("project")
    if not isinstance(engine, Mapping) or not isinstance(project, Mapping):
        raise ValueError("Wave 3 sidecar lacks engine/project identity")
    schema = sidecar.get("cache_schema")
    common = {
        "reference_alpha_hash": reference_alpha_hash,
        "operation_id": str(sidecar.get("operation_id", "")),
        "engine_semantic_version": str(engine.get("semantic_version", "")),
        "lean_version": str(project.get("lean_version", "")),
        "project_revision": str(project.get("project_revision", "")),
        "import_options_fingerprint": str(engine.get("import_options_fingerprint", "")),
    }
    if schema == 3:
        source_sha256 = str(engine.get("source_sha256", ""))
        if not _sha256_text(source_sha256):
            raise ValueError("Wave 3 schema-3 cache identity lacks engine source SHA-256")
        return SemanticCache.op_key(
            **common,
            name=str(sidecar.get("root_name", "")),
            engine_source_sha256=source_sha256,
        )
    if schema == 2:
        return SemanticCache.op_key(**common, name=str(sidecar.get("root_name", "")))
    if schema == 1:
        return legacy_op_key(**common)
    raise ValueError(f"Wave 3 cache schema {schema!r} is unsupported")


def wave3_cache_record_issues(
    sidecar: Mapping[str, Any],
    *,
    root_key: str,
    root_record: Mapping[str, Any],
    operation_key: str,
    operation_record: Mapping[str, Any],
) -> list[str]:
    """Bind one regular retained sidecar to its exact root and operation cache records."""

    issues: list[str] = []
    engine = sidecar.get("engine") or {}
    project = sidecar.get("project") or {}
    root_name = str(sidecar.get("root_name", ""))
    operation_id = str(sidecar.get("operation_id", ""))
    repr_block = sidecar.get("repr") or {}
    reference = repr_block.get("reference") or {}
    candidate = repr_block.get("candidate") or {}
    hashes = sidecar.get("lean_request_hashes") or {}
    try:
        expected_root_key = wave3_root_cache_key(sidecar)
    except (KeyError, TypeError, ValueError) as exc:
        issues.append(str(exc))
        expected_root_key = ""
    if root_key != expected_root_key:
        issues.append("root cache key differs from the sidecar identity")
    for field, expected in (
        ("name", root_name),
        ("project_revision", project.get("project_revision")),
        ("lean_version", project.get("lean_version")),
        ("engine", engine),
        ("reference_goal", reference.get("goal_v1")),
    ):
        if root_record.get(field) != expected:
            issues.append(f"root cache {field} differs")
    if root_record.get("root_status") != "ok":
        issues.append("root cache is not an elaborated ok record")
    alpha = root_record.get("reference_alpha_hash")
    if not isinstance(alpha, str) or not alpha:
        issues.append("root cache lacks the reference alpha hash")
    else:
        try:
            expected_operation_key = wave3_operation_cache_key(sidecar, reference_alpha_hash=alpha)
        except (KeyError, TypeError, ValueError) as exc:
            issues.append(str(exc))
            expected_operation_key = ""
        if operation_key != expected_operation_key:
            issues.append("operation cache key differs from the sidecar identity")
    if operation_key != sidecar.get("cache_key"):
        issues.append("operation cache key differs from sidecar cache_key")
    operations = root_record.get("ops") or {}
    if not isinstance(operations, Mapping) or operations.get(operation_id) != operation_key:
        issues.append("root cache does not bind the operation cache key")

    for field, expected in (
        ("root", root_name),
        ("operation_id", operation_id),
        ("label", sidecar.get("label")),
        ("status", "retained"),
        ("engine", engine),
        ("site", sidecar.get("site")),
        ("evidence", sidecar.get("evidence")),
        ("candidate_goal", candidate.get("goal_v1")),
        ("process_request_hash", hashes.get("process")),
    ):
        if operation_record.get(field) != expected:
            issues.append(f"operation cache {field} differs")
    render = operation_record.get("render") or {}
    if render.get("request_hash") != hashes.get("render"):
        issues.append("operation cache render request hash differs")
    for endpoint in ("reference", "candidate"):
        cached_endpoint = render.get(endpoint) or {}
        if cached_endpoint.get("record") != repr_block.get(endpoint):
            issues.append(f"operation cache {endpoint} representation differs")
        if cached_endpoint.get("source_material") != repr_block.get(f"{endpoint}_source_material"):
            issues.append(f"operation cache {endpoint} source material differs")
    return issues


def load_wave3_cache_snapshot(
    release_dir: Path, snapshot: object
) -> tuple[dict[tuple[str, str], dict[str, Any]], list[str]]:
    """Load and fully hash-check the packed Wave 3 cache snapshot."""

    issues: list[str] = []
    index: dict[tuple[str, str], dict[str, Any]] = {}
    if not isinstance(snapshot, Mapping):
        return index, ["source cache snapshot manifest is missing"]
    if snapshot.get("schema_version") != WAVE3_CACHE_SNAPSHOT_SCHEMA:
        issues.append("source cache snapshot schema is unsupported")
    name = snapshot.get("file")
    if name != WAVE3_CACHE_SNAPSHOT_FILE:
        issues.append("source cache snapshot path is not canonical")
        return index, issues
    root = release_dir.resolve()
    path = (root / str(name)).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        issues.append("source cache snapshot file is missing")
        return index, issues
    if hash_file(path) != snapshot.get("file_sha256"):
        issues.append("source cache snapshot file hash differs from the manifest")
    try:
        values = read_jsonl(path)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        issues.append(f"source cache snapshot is malformed: {type(exc).__name__}")
        return index, issues
    identities: list[dict[str, Any]] = []
    for offset, entry in enumerate(values):
        if set(entry) != {
            "schema_version",
            "kind",
            "key",
            "content_sha256",
            "sources",
            "record",
        }:
            issues.append(f"source cache snapshot entry {offset} has an unexpected schema")
            continue
        kind = entry.get("kind")
        key = entry.get("key")
        record = entry.get("record")
        sources = entry.get("sources")
        if (
            entry.get("schema_version") != WAVE3_CACHE_SNAPSHOT_SCHEMA
            or kind not in {"root", "operation"}
            or not _sha256_text(key)
            or not isinstance(record, Mapping)
            or not isinstance(sources, list)
            or not sources
        ):
            issues.append(f"source cache snapshot entry {offset} is malformed")
            continue
        source_items: list[dict[str, str]] = []
        for source in sources:
            if (
                not isinstance(source, Mapping)
                or set(source) != {"source_key", "file_sha256"}
                or not _sha256_text(source.get("source_key"))
                or not _sha256_text(source.get("file_sha256"))
            ):
                issues.append(f"source cache snapshot entry {offset} has malformed source proof")
                continue
            source_items.append(
                {
                    "source_key": str(source["source_key"]),
                    "file_sha256": str(source["file_sha256"]),
                }
            )
        if source_items != sorted(source_items, key=lambda item: tuple(item.values())) or len(
            source_items
        ) != len(sources):
            issues.append(f"source cache snapshot entry {offset} source proofs are not canonical")
        content_sha256 = hash_canonical(record)
        if entry.get("content_sha256") != content_sha256:
            issues.append(f"source cache snapshot entry {offset} content hash differs")
        identity = (str(kind), str(key))
        if identity in index:
            issues.append(f"source cache snapshot repeats {kind} key {key}")
        else:
            index[identity] = entry
        identities.append(
            {
                "kind": kind,
                "key": key,
                "content_sha256": entry.get("content_sha256"),
                "sources": sources,
            }
        )
    expected_counts = {
        "record_count": len(values),
        "root_records": sum(entry.get("kind") == "root" for entry in values),
        "operation_records": sum(entry.get("kind") == "operation" for entry in values),
    }
    for field, expected in expected_counts.items():
        if snapshot.get(field) != expected:
            issues.append(f"source cache snapshot {field} differs from its contents")
    if snapshot.get("content_set_sha256") != hash_canonical(identities):
        issues.append("source cache snapshot content-set hash differs")
    return index, issues


def wave3_cache_reference_issues(
    sidecar: Mapping[str, Any],
    *,
    release_id: object,
    snapshot_index: Mapping[tuple[str, str], Mapping[str, Any]],
) -> list[str]:
    """Check one release sidecar's links to its packed source-cache records."""

    issues: list[str] = []
    release = sidecar.get("release")
    if not isinstance(release, Mapping) or release.get("release_id") != release_id:
        return ["sidecar release identity differs from the manifest"]
    source = release.get("source")
    cache = release.get("source_cache")
    if not isinstance(source, Mapping) or not isinstance(cache, Mapping):
        return ["sidecar source/cache release proof is missing"]
    project = sidecar.get("project") or {}
    if source.get("project_id") != project.get("project_id") or source.get(
        "project_revision"
    ) != project.get("project_revision"):
        issues.append("sidecar release source differs from its project pin")
    source_key = source.get("source_key")
    refs: dict[str, Mapping[str, Any]] = {}
    for kind in ("root", "operation"):
        ref = cache.get(kind)
        if not isinstance(ref, Mapping) or set(ref) != {
            "key",
            "content_sha256",
            "source_file_sha256",
        }:
            issues.append(f"sidecar {kind} cache reference is malformed")
            continue
        refs[kind] = ref
        entry = snapshot_index.get((kind, str(ref.get("key", ""))))
        if entry is None:
            issues.append(f"sidecar {kind} cache snapshot is missing")
            continue
        if entry.get("content_sha256") != ref.get("content_sha256"):
            issues.append(f"sidecar {kind} cache content hash differs")
        expected_source = {
            "source_key": source_key,
            "file_sha256": ref.get("source_file_sha256"),
        }
        if expected_source not in (entry.get("sources") or []):
            issues.append(f"sidecar {kind} cache source-file proof differs")
    if "root" not in refs or "operation" not in refs:
        return issues
    root_entry = snapshot_index.get(("root", str(refs["root"].get("key", ""))))
    operation_entry = snapshot_index.get(("operation", str(refs["operation"].get("key", ""))))
    if root_entry is None or operation_entry is None:
        return issues
    root_record = root_entry.get("record")
    operation_record = operation_entry.get("record")
    if not isinstance(root_record, Mapping) or not isinstance(operation_record, Mapping):
        issues.append("sidecar cache snapshot records are malformed")
        return issues
    issues.extend(
        wave3_cache_record_issues(
            sidecar,
            root_key=str(refs["root"]["key"]),
            root_record=root_record,
            operation_key=str(refs["operation"]["key"]),
            operation_record=operation_record,
        )
    )
    return issues


def derive_wave3_snapshot_provenance(
    records: Sequence[Mapping[str, Any]],
    *,
    repo_root: Path,
    release_dir: Path,
    snapshot: object,
) -> tuple[dict[str, Any], list[str]]:
    """Run the existing provenance derivation against immutable packed root snapshots."""

    snapshot_index, issues = load_wave3_cache_snapshot(release_dir, snapshot)
    with tempfile.TemporaryDirectory(prefix="leanfaith-wave3-cache-") as temporary:
        cache = SemanticCache(Path(temporary))
        for (kind, key), entry in snapshot_index.items():
            record = entry.get("record")
            if kind == "root" and isinstance(record, Mapping):
                cache.put_root(key, record)
        provenance = derive_provenance(
            records,
            repo_root=repo_root,
            cache_root=cache.root,
            release_dir=release_dir,
            allow_multiple_project_pins=True,
        )
    return provenance, issues


def git_commit_is_ancestor(repo_root: Path, commit: object) -> bool:
    """Whether ``commit`` is a full Git revision reachable from the release revision."""

    if (
        not isinstance(commit, str)
        or len(commit) != 40
        or any(character not in "0123456789abcdef" for character in commit)
    ):
        return False
    result = subprocess.run(
        ["git", "-C", str(repo_root), "merge-base", "--is-ancestor", commit, "HEAD"],
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def _source_run_receipt_issues(
    source_run: object, staging_root: Path, repo_root: Path
) -> list[str]:
    """Validate either a legacy run id or an embedded immutable Wave 3 receipt."""

    if isinstance(source_run, str):
        run_dir = staging_root / "runs" / source_run
        run_id = source_run
        expected_hashes: Mapping[str, Any] | None = None
    elif isinstance(source_run, Mapping):
        run_dir_value = source_run.get("run_dir")
        run_id_value = source_run.get("run_id")
        if not isinstance(run_dir_value, str) or not isinstance(run_id_value, str):
            return ["embedded source run receipt lacks run_dir/run_id"]
        run_dir = Path(run_dir_value)
        run_id = run_id_value
        hashes = source_run.get("input_sha256")
        expected_hashes = hashes if isinstance(hashes, Mapping) else None
    else:
        return ["source run receipt is neither a run id nor an object"]

    issues: list[str] = []
    observed: dict[str, str] = {}
    for receipt_name, filename in _RUN_RECEIPT_FILES.items():
        path = run_dir / filename
        if not path.is_file():
            issues.append(f"{run_id} missing {filename}")
            continue
        observed[receipt_name] = hash_file(path)
        if (
            expected_hashes is not None
            and expected_hashes.get(receipt_name) != observed[receipt_name]
        ):
            issues.append(f"{run_id} {filename} hash differs from its receipt")
    status_path = run_dir / "status.json"
    status = read_json_object(status_path) if status_path.is_file() else {}
    if status.get("run_id") != run_id or status.get("final") is not True:
        issues.append(f"{run_id} status.json is not the recorded final run")
    replay_path = run_dir / "replay_report.json"
    replay = read_json_object(replay_path) if replay_path.is_file() else None
    if replay is None:
        issues.append(f"{run_id} missing replay_report.json")
    elif (
        replay.get("run_id") != run_id
        or replay.get("lean_requests") != 0
        or replay.get("duplicate_rows") != 0
    ):
        issues.append(f"{run_id} replay issued Lean requests, appended rows, or changed run id")
    if isinstance(source_run, Mapping):
        manifest_path = run_dir / "run.json"
        run_manifest = read_json_object(manifest_path) if manifest_path.is_file() else {}
        if run_manifest.get("implementation_dirty") is not False:
            issues.append(f"{run_id} generator worktree was dirty")
        generator_commit = run_manifest.get("implementation_commit")
        if not git_commit_is_ancestor(repo_root, generator_commit):
            issues.append(f"{run_id} generator commit is not an ancestor of the release")
        if (
            source_run.get("implementation_dirty") != run_manifest.get("implementation_dirty")
            or source_run.get("implementation_commit") != generator_commit
        ):
            issues.append(f"{run_id} embedded generator identity differs from run.json")
        checks = source_run.get("checks")
        if (
            not isinstance(checks, Mapping)
            or not checks
            or not all(value is True for value in checks.values())
        ):
            issues.append(f"{run_id} embedded release-authorization checks did not all pass")
        if expected_hashes is None:
            issues.append(f"{run_id} embedded receipt lacks input hashes")
        project_id = source_run.get("project_id")
        project_revision = source_run.get("project_revision")
        source_key = source_run.get("source_key")
        if (
            isinstance(project_id, str)
            and isinstance(project_revision, str)
            and len(observed) == len(_RUN_RECEIPT_FILES)
        ):
            filename_hashes = {
                filename: observed[receipt_name]
                for receipt_name, filename in _RUN_RECEIPT_FILES.items()
            }
            expected_source_key = hash_canonical(
                [project_id, project_revision, run_id, filename_hashes]
            )
            if source_key != expected_source_key:
                issues.append(f"{run_id} source key differs from its immutable file receipt")
    return issues


def _wave4_release_pair_id(sidecar: Mapping[str, Any], row: Mapping[str, Any]) -> str:
    """Recompute the stable physical-pair identity used by ``Wave4Runner``."""

    row_kind = str(sidecar.get("row_kind", ""))
    wave4 = sidecar.get("wave4") or {}
    negative = str(sidecar.get("negative_operation", ""))
    chain_hash: object
    if row_kind == "negative_base":
        chain_hash = hash_canonical(
            {
                "kind": "sft1_wave4_base_negative_chain_v1",
                "negative_operation": negative,
            }
        )
    elif row_kind == "preserving_reference":
        chain_hash = wave4.get("reference_chain_hash")
    elif row_kind == "preserving_candidate":
        chain_hash = wave4.get("candidate_chain_hash")
    elif row_kind == "negative_last":
        chain_hash = hash_canonical(
            [wave4.get("reference_chain_hash"), wave4.get("candidate_chain_hash")]
        )
    else:
        chain_hash = None
    repr_block = sidecar.get("repr") or {}
    reference = (repr_block.get("reference") or {}).get("provenance") or {}
    candidate = (repr_block.get("candidate") or {}).get("provenance") or {}
    site = sidecar.get("site") or {}
    return make_id(
        PAIR_PREFIX,
        {
            "kind": "sft1_wave4_physical_pair_v1",
            "root_id": sidecar.get("root_id"),
            "operation_id": sidecar.get("operation_id"),
            "negative_operation": negative,
            "row_kind": row_kind,
            "reference_expr_hash": reference.get("expr_hash"),
            "candidate_expr_hash": candidate.get("expr_hash"),
            "label": row.get("label"),
            "operation_chain_hash": chain_hash,
            "selected_site_hash": site.get("detail"),
            "evidence_hash": sidecar.get("evidence_hash"),
        },
    )


def _wave4_row_evidence_issue(sidecar: Mapping[str, Any]) -> str | None:
    """Cheap independent checks for the row-local certificate selected by its role."""

    kind = str(sidecar.get("row_kind", ""))
    evidence = sidecar.get("evidence") or {}
    row_check = sidecar.get("row_check") or {}
    if not (row_check.get("meta_checked") and row_check.get("kernel_checked")):
        return "wave4_row_without_checked_certificate"
    if sidecar.get("evidence_hash") != hash_canonical(evidence):
        return "wave4_evidence_hash"
    if kind in {"preserving_reference", "preserving_candidate"}:
        check = (evidence.get("equivalence_proof") or {}).get("check") or {}
        if not (check.get("meta_checked") and check.get("kernel_checked")):
            return "wave4_positive_without_checked_equivalence"
        if check != row_check:
            return "wave4_positive_check_mismatch"
        return None
    refutation = evidence.get("refutation") or {}
    check = refutation.get("check") or {}
    if (
        refutation.get("goal") != "Not (Iff reference candidate)"
        or not check.get("meta_checked")
        or not check.get("kernel_checked")
    ):
        return "wave4_negative_without_checked_refutation"
    if check != row_check:
        return "wave4_negative_check_mismatch"
    if not isinstance(evidence.get("negative_family_evidence"), Mapping):
        return "wave4_negative_family_evidence_missing"
    if kind == "negative_base":
        source = evidence.get("source_proof_check") or {}
        if not (source.get("meta_checked") and source.get("kernel_checked")):
            return "wave4_base_without_checked_source_proof"
    elif kind == "negative_last" and not isinstance(evidence.get("negative_last_replay"), Mapping):
        return "wave4_negative_last_replay_missing"
    return None


def _wave4_source_sidecar(sidecar: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize release-only closure membership and snapshot fields for source matching."""

    result = {key: value for key, value in sidecar.items() if key != "release"}
    result.pop("closure_group_ids", None)
    cache = result.get("cache")
    if isinstance(cache, Mapping):
        result["cache"] = {**cache, "snapshot": None}
    return result


def _safe_wave4_artifact(release_dir: Path, relative: object) -> Path | None:
    if not isinstance(relative, str) or not relative:
        return None
    value = Path(relative)
    if value.is_absolute() or ".." in value.parts:
        return None
    root = release_dir.resolve()
    path = (root / value).resolve()
    return path if path.is_relative_to(root) else None


def _validate_wave4_release(
    *,
    repo_root: Path,
    staging_root: Path,
    run_id: str,
    compacted_dir: Path,
    manifest: Mapping[str, Any],
    source_runs: Sequence[str | Mapping[str, Any]],
) -> dict[str, Any]:
    """Independently validate one immutable Wave 4 multi-project release."""

    from leanfaith.sft1.sprint.square import (
        WAVE4_OPERATIONS,
        _wave4_closure_edge_issues,
        _wave4_group_aggregates,
        _wave4_row_hash,
        load_completed_wave4_run,
        materialize_wave4_records,
    )

    issues: list[str] = []
    issue_counts: dict[str, int] = {}

    def issue(text: str) -> None:
        if len(issues) < 200:
            issues.append(text)
        key = text.split(":", 1)[0]
        issue_counts[key] = issue_counts.get(key, 0) + 1

    release_id = manifest.get("release_id")
    policy_hash = manifest.get("policy_hash")
    if not isinstance(policy_hash, str) or not _sha256_text(policy_hash):
        issue("manifest_policy: Wave 4 policy hash is missing or malformed")
        policy_hash = ""
    builder = manifest.get("release_builder")
    if not isinstance(builder, Mapping) or builder.get("dirty") is not False:
        issue("release_builder: Wave 4 release builder worktree was dirty")
    elif not git_commit_is_ancestor(repo_root, builder.get("commit")):
        issue("release_builder: Wave 4 release builder commit is not an ancestor")

    effective_runs: list[str | Mapping[str, Any]]
    if source_runs:
        effective_runs = list(source_runs)
    else:
        raw_runs = manifest.get("source_runs")
        effective_runs = list(raw_runs) if isinstance(raw_runs, list) else []
    if manifest.get("source_receipts_sha256") != hash_canonical(effective_runs):
        issue("source_run_receipt: source receipt-set hash differs from the manifest")
    receipts = [item for item in effective_runs if isinstance(item, Mapping)]
    if len(receipts) != len(effective_runs) or not receipts:
        issue("source_run_receipt: Wave 4 requires embedded immutable run receipts")
    receipt_by_key: dict[str, Mapping[str, Any]] = {}
    source_records: dict[str, list[tuple[Mapping[str, Any], str, str]]] = {}
    source_group_hashes: set[str] = set()
    for receipt in receipts:
        source_key = receipt.get("source_key")
        run_dir_value = receipt.get("run_dir")
        if not isinstance(source_key, str) or source_key in receipt_by_key:
            issue("source_run_receipt: source keys are missing or repeated")
            continue
        receipt_by_key[source_key] = receipt
        if not isinstance(run_dir_value, str):
            issue(f"source_run_receipt: {source_key} lacks run_dir")
            continue
        try:
            rebuilt, bundle = load_completed_wave4_run(
                repo_root, Path(run_dir_value), policy_hash=policy_hash
            )
        except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            issue(f"source_run_receipt: {source_key} cannot be replayed: {exc}")
            continue
        if hash_canonical(rebuilt) != hash_canonical(receipt):
            issue(f"source_run_receipt: {source_key} differs from independently rebuilt receipt")
        for item in bundle.rows:
            sidecar = item.get("sidecar") or {}
            pair_id = str(sidecar.get("pair_id", ""))
            source_records.setdefault(pair_id, []).append((item, hash_canonical(item), source_key))
        source_group_hashes.update(hash_canonical(group.record) for group in bundle.groups)
    if {str(item.get("project_id")) for item in receipts} != WAVE4_PROJECTS:
        issue("source_run_receipt: Wave 4 does not bind all three pinned projects")
    if any(item.get("policy_hash") != policy_hash for item in receipts):
        issue("source_run_receipt: Wave 4 source runs do not share the manifest policy")

    retained_files = manifest.get("source_retained_files")
    if not isinstance(retained_files, list) or len(retained_files) != len(receipts):
        issue("source_retained_manifest: Wave 4 retained-file receipt count differs")
    else:
        retained_keys: set[str] = set()
        retained_paths: list[str] = []
        for entry in retained_files:
            if not isinstance(entry, Mapping):
                issue("source_retained_manifest: malformed retained-file receipt")
                continue
            source_key = entry.get("source_key")
            source_receipt = receipt_by_key.get(str(source_key))
            path_value = entry.get("path")
            if not isinstance(path_value, str) or source_receipt is None:
                issue("source_retained_manifest: retained file names an unknown source")
                continue
            path = Path(path_value)
            if not path.is_absolute():
                path = staging_root / path
            retained_paths.append(str(path))
            retained_keys.add(str(source_key))
            hashes = source_receipt.get("input_sha256") or {}
            expected_path = str(Path(str(source_receipt.get("run_dir"))) / "retained.jsonl")
            if (
                entry.get("run_id") != source_receipt.get("run_id")
                or entry.get("project_id") != source_receipt.get("project_id")
                or entry.get("path") != expected_path
                or entry.get("sha256") != hashes.get("retained")
                or not path.is_file()
                or hash_file(path) != entry.get("sha256")
            ):
                issue(f"source_retained_manifest: {source_key} retained file differs")
        if retained_keys != set(receipt_by_key):
            issue("source_retained_manifest: source keys differ from source-run receipts")
        if manifest.get("source_retained_paths") != retained_paths:
            issue("source_retained_manifest: path list differs from retained-file receipts")

    source_cache_entries = manifest.get("source_cache_files")
    source_cache_index: dict[tuple[str, str], Mapping[str, Any]] = {}
    if not isinstance(source_cache_entries, list) or not source_cache_entries:
        issue("source_cache_file: Wave 4 lacks exact source-cache file receipts")
    else:
        for entry in source_cache_entries:
            if not isinstance(entry, Mapping):
                issue("source_cache_file: malformed source-cache receipt")
                continue
            source_key = str(entry.get("source_key", ""))
            path_value = entry.get("path")
            if source_key not in receipt_by_key or not isinstance(path_value, str):
                issue("source_cache_file: source-cache receipt names an unknown source")
                continue
            identity = (source_key, path_value)
            if identity in source_cache_index:
                issue("source_cache_file: duplicate source-cache receipt")
                continue
            source_cache_index[identity] = entry
            path = Path(path_value)
            try:
                cached = read_json_object(path) if path.is_file() else None
            except (OSError, ValueError, json.JSONDecodeError):
                cached = None
            if (
                cached is None
                or hash_file(path) != entry.get("sha256")
                or hash_canonical(cached) != entry.get("content_sha256")
            ):
                issue(f"source_cache_file: {source_key} cache record differs: {path_value}")

    snapshot_entries = manifest.get("cache_snapshots")
    snapshot_records = 0
    snapshot_files = 0
    if not isinstance(snapshot_entries, list) or not snapshot_entries:
        issue("cache_snapshot: Wave 4 lacks immutable cache snapshots")
    else:
        seen_snapshot_files: set[str] = set()
        for entry in snapshot_entries:
            if not isinstance(entry, Mapping):
                issue("cache_snapshot: malformed cache snapshot manifest")
                continue
            name = entry.get("file")
            snapshot_path = _safe_wave4_artifact(compacted_dir, name)
            if not isinstance(name, str) or name in seen_snapshot_files or snapshot_path is None:
                issue("cache_snapshot: unsafe or repeated cache snapshot path")
                continue
            seen_snapshot_files.add(name)
            if not snapshot_path.is_file():
                issue(f"cache_snapshot: missing {name}")
                continue
            snapshot_files += 1
            if hash_file(snapshot_path) != entry.get("sha256"):
                issue(f"cache_snapshot: file hash differs for {name}")
            try:
                values = read_jsonl(snapshot_path)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                issue(f"cache_snapshot: malformed {name}: {type(exc).__name__}")
                continue
            snapshot_records += len(values)
            if entry.get("records") != len(values) or entry.get("squares") != len(values):
                issue(f"cache_snapshot: record count differs for {name}")
            content_hashes = [hash_canonical(value) for value in values]
            if len(content_hashes) != len(set(content_hashes)):
                issue(f"cache_snapshot: duplicate cache record in {name}")
            if entry.get("content_set_sha256") != hash_canonical(sorted(content_hashes)):
                issue(f"cache_snapshot: content-set hash differs for {name}")

    declared_shards = manifest.get("shards")
    if not isinstance(declared_shards, list) or not declared_shards:
        issue("shard_manifest: Wave 4 release declares no shards")
        declared_shards = []
    records: list[dict[str, Any]] = []
    group_records: list[dict[str, Any]] = []
    seen_pairs: set[str] = set()
    seen_unordered: dict[str, tuple[str, bool]] = {}
    seen_groups: set[str] = set()
    project_counts: dict[str, int] = {}
    operation_counts: dict[str, int] = {}
    mechanism_counts: dict[str, int] = {}
    positive = 0
    for shard_entry in declared_shards:
        if not isinstance(shard_entry, Mapping):
            issue("shard_manifest: malformed top-level shard entry")
            continue
        number = shard_entry.get("shard")
        if type(number) is not int or number < 1:
            issue("shard_manifest: invalid shard number")
            continue
        shard_dir = compacted_dir / f"shard-{number:04d}"
        shard_manifest_path = shard_dir / "manifest.json"
        try:
            shard_manifest = read_json_object(shard_manifest_path)
        except (OSError, ValueError, json.JSONDecodeError):
            issue(f"shard_manifest: missing or malformed shard {number}")
            continue
        if hash_canonical(shard_manifest) != hash_canonical(shard_entry):
            issue(f"shard_manifest: shard {number} differs from top-level declaration")
        paths = {
            "rows": shard_dir / "rows.jsonl",
            "sidecars": shard_dir / "sidecars.jsonl",
            "groups": shard_dir / "closure_groups.jsonl",
        }
        if any(not path.is_file() for path in paths.values()):
            issue(f"shard_files: shard {number} is incomplete")
            continue
        if hash_file(paths["rows"]) != shard_manifest.get("rows_sha256"):
            issue(f"shard_rows_hash: shard {number}")
        if hash_file(paths["sidecars"]) != shard_manifest.get("sidecars_sha256"):
            issue(f"shard_sidecars_hash: shard {number}")
        if hash_file(paths["groups"]) != shard_manifest.get("closure_groups_sha256"):
            issue(f"shard_groups_hash: shard {number}")
        try:
            rows = read_jsonl(paths["rows"])
            sidecars = read_jsonl(paths["sidecars"])
            groups = read_jsonl(paths["groups"])
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            issue(f"shard_files: shard {number} is malformed: {type(exc).__name__}")
            continue
        if len(rows) != len(sidecars) or len(rows) != shard_manifest.get("row_count"):
            issue(f"shard_row_count: shard {number}")
        if len(groups) != shard_manifest.get("logical_group_count"):
            issue(f"shard_group_count: shard {number}")
        if shard_manifest.get("logical_row_count") != len(groups) * len(WAVE4_ROW_LABELS):
            issue(f"shard_logical_row_count: shard {number}")
        for group in groups:
            group_id = str(group.get("group_id", ""))
            if not group_id or group_id in seen_groups:
                issue(f"duplicate_group_id: {group_id or '<missing>'}")
            seen_groups.add(group_id)
            if hash_canonical(group) not in source_group_hashes:
                issue(f"source_group_binding: {group_id}")
            group_records.append(group)
        for row, sidecar in zip(rows, sidecars, strict=False):
            pair_id = str(sidecar.get("pair_id", ""))
            if set(row) != MODEL_FACING_ROW_FIELDS:
                issue(f"row_schema: {pair_id}")
            if pair_id in seen_pairs:
                issue(f"duplicate_pair_id: {pair_id}")
            seen_pairs.add(pair_id)
            row_kind = str(sidecar.get("row_kind", ""))
            expected_label_value = WAVE4_ROW_LABELS.get(row_kind)
            if expected_label_value is None or row.get("label") is not expected_label_value:
                issue(f"label_polarity: {pair_id}")
            if sidecar.get("label") is not row.get("label"):
                issue(f"sidecar_label: {pair_id}")
            operation = str(sidecar.get("operation_id", ""))
            negative = str(sidecar.get("negative_operation", ""))
            if operation not in WAVE4_OPERATIONS:
                issue(f"operation_identity: {pair_id}")
            try:
                expected_mechanism = mechanism_of(negative)
            except KeyError:
                expected_mechanism = None
            if sidecar.get("mechanism") != expected_mechanism:
                issue(f"mechanism_metadata: {pair_id}")
            evidence_issue = _wave4_row_evidence_issue(sidecar)
            if evidence_issue is not None:
                issue(f"{evidence_issue}: {pair_id}")
            repr_block = sidecar.get("repr") or {}
            reference = repr_block.get("reference") or {}
            candidate = repr_block.get("candidate") or {}
            if reference.get("goal_v1") != row.get("reference") or candidate.get(
                "goal_v1"
            ) != row.get("candidate"):
                issue(f"repr_text_mismatch: {pair_id}")
            if reference.get("rendered_goal_hash") != sha256_hex(
                str(row.get("reference", "")).encode("utf-8")
            ):
                issue(f"reference_render_hash: {pair_id}")
            if candidate.get("rendered_goal_hash") != sha256_hex(
                str(row.get("candidate", "")).encode("utf-8")
            ):
                issue(f"candidate_render_hash: {pair_id}")
            if row.get("reference") == row.get("candidate"):
                issue(f"self_pair: {pair_id}")
            for field in ("reference", "candidate"):
                violation = residue_violation(str(row.get(field, "")))
                if violation:
                    issue(f"residue_{violation}: {pair_id}")
            unordered = unordered_pair_key(
                str(reference.get("rendered_goal_hash")),
                str(candidate.get("rendered_goal_hash")),
            )
            owner = seen_unordered.get(unordered)
            pair_identity = (pair_id, bool(row.get("label")))
            if owner is not None and owner != pair_identity:
                issue(f"duplicate_or_conflicting_unordered_pair: {pair_id}")
            seen_unordered[unordered] = pair_identity
            if _wave4_release_pair_id(sidecar, row) != pair_id:
                issue(f"pair_id_recompute: {pair_id}")
            release = sidecar.get("release")
            if not isinstance(release, Mapping) or release.get("release_id") != release_id:
                issue(f"release_binding: {pair_id}")
                release = {}
            if release.get("schema_version") != WAVE4_RELEASE_SCHEMA:
                issue(f"release_schema: {pair_id}")
            if release.get("release_row_hash") != _wave4_row_hash(row, sidecar):
                issue(f"release_row_hash: {pair_id}")
            source = release.get("source") or {}
            source_key = str(source.get("source_key", ""))
            project = sidecar.get("project") or {}
            if source.get("project_id") != project.get("project_id") or source.get(
                "project_revision"
            ) != project.get("project_revision"):
                issue(f"release_source_project: {pair_id}")
            candidates = source_records.get(pair_id, [])
            matched = next(
                (
                    item
                    for item in candidates
                    if item[1] == release.get("source_record_sha256") and item[2] == source_key
                ),
                None,
            )
            if matched is None:
                issue(f"source_record_binding: {pair_id}")
            else:
                source_item = matched[0]
                if source_item.get("row") != row or hash_canonical(
                    _wave4_source_sidecar(source_item.get("sidecar") or {})
                ) != hash_canonical(_wave4_source_sidecar(sidecar)):
                    issue(f"source_record_content: {pair_id}")
            source_cache = release.get("source_cache_file") or {}
            cache_identity = (source_key, str(source_cache.get("path", "")))
            cache_receipt = source_cache_index.get(cache_identity)
            if (
                cache_receipt is None
                or cache_receipt.get("sha256") != source_cache.get("file_sha256")
                or cache_receipt.get("content_sha256") != source_cache.get("content_sha256")
                or source_cache.get("content_sha256")
                != (sidecar.get("cache") or {}).get("content_sha256")
            ):
                issue(f"source_cache_binding: {pair_id}")
            project_id = str(source.get("project_id", "missing"))
            project_counts[project_id] = project_counts.get(project_id, 0) + 1
            operation_counts[operation] = operation_counts.get(operation, 0) + 1
            mechanism = str(sidecar.get("mechanism", ""))
            mechanism_counts[mechanism] = mechanism_counts.get(mechanism, 0) + 1
            positive += int(bool(row.get("label")))
            records.append(
                {
                    "row": row,
                    "sidecar": sidecar,
                    "row_hash": release.get("release_row_hash"),
                    "unordered_pair_key": unordered,
                }
            )

    unexpected_shards = {
        path.name for path in compacted_dir.glob("shard-*") if path.is_dir()
    }.difference(
        {
            f"shard-{int(item['shard']):04d}"
            for item in declared_shards
            if isinstance(item, Mapping) and type(item.get("shard")) is int
        }
    )
    if unexpected_shards:
        issue("shard_manifest: undeclared shard directories exist")

    materialized = None
    try:
        materialized = materialize_wave4_records(records, group_records)
    except (ValueError, KeyError, TypeError) as exc:
        issue(f"certificate_closure: {exc}")
    if materialized is not None:
        for text in _wave4_closure_edge_issues(materialized):
            issue(f"certificate_closure: {text}")
        aggregates = _wave4_group_aggregates(materialized.groups)
        for field, expected in aggregates.items():
            if manifest.get(field) != expected:
                issue(f"manifest_aggregate: {field} differs")
        n25_group_ids = {
            group.group_id
            for group in materialized.groups
            if group.operation_id == "N25_TOGGLE_EQ_NE_PROOF_V1"
        }
        n25_pair_ids = {
            pair_id
            for group in materialized.groups
            if group.group_id in n25_group_ids
            for pair_id in group.row_ids
        }
        negative_share = manifest.get("negative_share_cap") or {}
        if (
            negative_share.get("operation_id") != "N25_TOGGLE_EQ_NE_PROOF_V1"
            or negative_share.get("operation_selected_group_count") != len(n25_group_ids)
            or negative_share.get("operation_selected_row_count") != len(n25_pair_ids)
            or not isinstance(negative_share.get("maximum_operation_row_count"), int)
            or len(n25_pair_ids) > negative_share.get("maximum_operation_row_count", -1)
        ):
            issue("negative_share_cap: N25 released-row cap differs")
        if any(
            group.operation_id == "N19_WHOLE_CLAIM_NEGATION_V1" for group in materialized.groups
        ):
            issue("negative_family: N19 is forbidden")
        if manifest.get("logical_groups") != len(materialized.groups):
            issue("manifest_aggregate: logical group count differs")
        if manifest.get("logical_rows") != materialized.logical_row_count:
            issue("manifest_aggregate: logical row count differs")
    if manifest.get("retained_rows") != len(records):
        issue("manifest_aggregate: retained row count differs")
    roots = len({str(item["sidecar"].get("root_id")) for item in records})
    if manifest.get("roots") != roots:
        issue("manifest_aggregate: root count differs")
    labels = {"positive": positive, "negative": len(records) - positive}
    for field, value in (
        ("labels", labels),
        ("projects", dict(sorted(project_counts.items()))),
        ("operations", dict(sorted(operation_counts.items()))),
        ("mechanisms", dict(sorted(mechanism_counts.items()))),
    ):
        if manifest.get(field) != value:
            issue(f"manifest_aggregate: {field} differs")
    if set(project_counts) != WAVE4_PROJECTS:
        issue("manifest_projects: released rows do not cover all three pinned projects")

    for field in ("shortcut_screens", "pairwise_diagnostics"):
        declaration = manifest.get(field)
        if not isinstance(declaration, Mapping):
            issue(f"{field}: missing artifact declaration")
            continue
        artifact_path = _safe_wave4_artifact(compacted_dir, declaration.get("file"))
        if (
            artifact_path is None
            or not artifact_path.is_file()
            or hash_file(artifact_path) != declaration.get("sha256")
        ):
            issue(f"{field}: artifact hash differs")
            continue
        if field == "shortcut_screens":
            try:
                screens = read_json_object(artifact_path)
            except (OSError, ValueError, json.JSONDecodeError):
                issue("shortcut_screens: artifact is malformed")
                continue
            by_name = {
                str(item.get("name")): item
                for item in screens.get("screens") or []
                if isinstance(item, Mapping)
            }
            if any(
                by_name.get(name, {}).get("passed") is not True
                for name in ("candidate_only", "reference_only", "family_held_out")
            ):
                issue("shortcut_screens: a required screen did not pass")

    for field in ("screen_rejections", "capacity_dropped_groups"):
        declaration = manifest.get(field)
        if not isinstance(declaration, Mapping):
            issue(f"{field}: missing ledger declaration")
            continue
        ledger_path = _safe_wave4_artifact(compacted_dir, declaration.get("file"))
        if (
            ledger_path is None
            or not ledger_path.is_file()
            or hash_file(ledger_path) != declaration.get("sha256")
        ):
            issue(f"{field}: ledger hash differs")
    negative_share = manifest.get("negative_share_cap") or {}
    share_path = _safe_wave4_artifact(compacted_dir, negative_share.get("dropped_group_ids_file"))
    if (
        share_path is None
        or not share_path.is_file()
        or hash_file(share_path) != negative_share.get("dropped_group_ids_sha256")
    ):
        issue("negative_share_cap: drop ledger hash differs")

    release_mode = manifest.get("release_mode")
    if release_mode == "composition_gate_200":
        if roots != 200:
            issue("composition_gate: candidate does not contain exactly 200 roots")
        inspection = manifest.get("manual_inspection")
        if not isinstance(inspection, Mapping) or inspection.get("passed") is not True:
            issue("composition_gate: manual inspection receipt did not pass")
        else:
            for receipt in inspection.get("receipts") or []:
                if not isinstance(receipt, Mapping):
                    issue("composition_gate: malformed inspection receipt")
                    continue
                verdict = Path(str(receipt.get("path", "")))
                sample = Path(str(receipt.get("sample_path", "")))
                if (
                    not verdict.is_file()
                    or hash_file(verdict) != receipt.get("sha256")
                    or not sample.is_file()
                    or hash_file(sample) != receipt.get("sample_sha256")
                    or receipt.get("passed") is not True
                ):
                    issue("composition_gate: inspection evidence changed")
    elif release_mode == "full":
        gate = manifest.get("composition_gate")
        if not isinstance(gate, Mapping):
            issue("composition_gate: full release lacks passed 200-root gate")
        else:
            gate_path = _safe_wave4_artifact(compacted_dir, gate.get("file"))
            try:
                gate_document = (
                    read_json_object(gate_path)
                    if gate_path is not None and gate_path.is_file()
                    else None
                )
            except (OSError, ValueError, json.JSONDecodeError):
                gate_document = None
            gate_file_hash = (
                hash_file(gate_path) if gate_path is not None and gate_path.is_file() else None
            )
            if (
                gate_document is None
                or gate_file_hash != gate.get("sha256")
                or gate.get("source_sha256") != gate.get("sha256")
                or gate_document.get("kind") != "sft1_wave4_composition_gate_v1"
                or gate_document.get("policy_hash") != policy_hash
                or gate_document.get("unique_ancestry_roots") != 200
                or gate_document.get("passed") is not True
                or not isinstance(gate_document.get("checks"), Mapping)
                or not gate_document.get("checks")
                or not all(value is True for value in gate_document["checks"].values())
            ):
                issue("composition_gate: copied gate report is invalid or unbound")
            elif gate_document.get("content_binding_sha256") != hash_canonical(
                {
                    key: value
                    for key, value in gate_document.items()
                    if key != "content_binding_sha256"
                }
            ):
                issue("composition_gate: gate report content binding differs")
    else:
        issue("composition_gate: unknown Wave 4 release mode")

    provenance = derive_provenance(
        records,
        repo_root=repo_root,
        cache_root=compacted_dir / "no_live_cache",
        release_dir=compacted_dir,
        allow_multiple_project_pins=True,
    )
    if not provenance.get("consistent"):
        for text in provenance.get("issues") or []:
            issue(f"provenance: {text}")
    if hash_canonical(manifest.get("provenance")) != hash_canonical(provenance):
        issue("manifest_provenance: recorded provenance differs from sidecar derivation")
    if manifest.get("finalized") is not True or any(
        not isinstance(item, Mapping)
        or item.get("complete") is not True
        or item.get("finalized") is not True
        for item in declared_shards
    ):
        issue("finalized_shard: release or shard is not finalized")

    report = {
        "schema_version": 3,
        "run_id": run_id,
        "compacted_dir": ".",
        "manifest_sha256": hash_file(compacted_dir / "manifest.json"),
        "rows_checked": len(records),
        "logical_groups_checked": len(group_records),
        "shards": len(declared_shards),
        "source_retained_files_checked": len(retained_files or []),
        "source_cache_files_checked": len(source_cache_index),
        "source_cache_snapshot_records_checked": snapshot_records,
        "source_cache_snapshot_files_checked": snapshot_files,
        "issue_counts": issue_counts,
        "issues": issues,
        "provenance": provenance,
        "replay_semantics": REPLAY_SEMANTICS,
        "proof_check_time": "original_generation",
        "passed": not issues,
    }
    write_atomic(compacted_dir / "integrity_report.json", canonical_json_bytes(report) + b"\n")
    return report


def _wave3_gate_issues(
    *,
    repo_root: Path,
    staging_root: Path,
    release_dir: Path,
    manifest: Mapping[str, Any],
) -> list[str]:
    """Verify the immutable gate report and every external receipt it binds."""

    issues: list[str] = []
    declaration = manifest.get("wave3_gate")
    if not isinstance(declaration, Mapping):
        return ["Wave 3 manifest lacks its release-gate declaration"]
    path = _safe_wave4_artifact(release_dir, declaration.get("file"))
    if path is None or not path.is_file():
        return ["Wave 3 release-gate report is missing or unsafe"]
    if hash_file(path) != declaration.get("sha256"):
        issues.append("Wave 3 release-gate report hash differs")
    try:
        document = read_json_object(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return [*issues, "Wave 3 release-gate report is malformed"]
    content_binding = document.get("content_binding_sha256")
    without_binding = {
        key: value for key, value in document.items() if key != "content_binding_sha256"
    }
    if content_binding != hash_canonical(without_binding):
        issues.append("Wave 3 release-gate content binding differs")
    checks = document.get("checks")
    claimed_passed = (
        isinstance(checks, Mapping)
        and bool(checks)
        and all(value is True for value in checks.values())
    )
    if (
        document.get("schema_version") != 1
        or document.get("kind") != "sft1_wave3_release_gate_v1"
        or document.get("release_id") != manifest.get("release_id")
        or document.get("passed") is not claimed_passed
        or declaration.get("passed") is not document.get("passed")
        or declaration.get("content_binding_sha256") != content_binding
    ):
        issues.append("Wave 3 release-gate identity or pass state differs")

    families = document.get("family_gates")
    family_receipts = families.get("receipts") if isinstance(families, Mapping) else []
    released_pair_ids: set[str] = set()
    for shard in manifest.get("shards") or []:
        if not isinstance(shard, Mapping) or type(shard.get("shard")) is not int:
            continue
        sidecars_path = release_dir / f"shard-{int(shard['shard']):04d}" / "sidecars.jsonl"
        if not sidecars_path.is_file():
            continue
        try:
            released_pair_ids.update(
                str(sidecar["pair_id"])
                for sidecar in read_jsonl(sidecars_path)
                if isinstance(sidecar.get("pair_id"), str)
            )
        except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
            issues.append("Wave 3 released sidecars are malformed")
    if isinstance(family_receipts, list):
        for receipt in family_receipts:
            if not isinstance(receipt, Mapping):
                issues.append("Wave 3 family-gate receipt is malformed")
                continue
            run_receipt = receipt.get("run_receipt")
            if not isinstance(run_receipt, Mapping) or receipt.get(
                "run_receipt_sha256"
            ) != hash_canonical(run_receipt):
                issues.append("Wave 3 family-gate run receipt hash differs")
            else:
                for text in _source_run_receipt_issues(run_receipt, staging_root, repo_root):
                    issues.append(f"Wave 3 family-gate run receipt: {text}")
            sample_pair_ids = receipt.get("sample_pair_ids_list")
            normalized_sample_pair_ids = (
                [pair_id for pair_id in sample_pair_ids if isinstance(pair_id, str)]
                if isinstance(sample_pair_ids, list)
                else []
            )
            sample_pair_ids_valid = (
                isinstance(sample_pair_ids, list)
                and len(normalized_sample_pair_ids) == len(sample_pair_ids)
                and len(sample_pair_ids) == len(set(normalized_sample_pair_ids))
                and len(sample_pair_ids) == receipt.get("sample_pair_ids")
                and hash_canonical(sorted(normalized_sample_pair_ids))
                == receipt.get("sample_pair_ids_sha256")
            )
            sample_selection_bound = sample_pair_ids_valid and set(
                normalized_sample_pair_ids
            ).issubset(released_pair_ids)
            if (
                receipt.get("sample_selection_bound") is not sample_selection_bound
                or not sample_selection_bound
            ):
                issues.append("Wave 3 inspected pairs are not bound to released sidecars")
            for path_field, hash_field, label in (
                ("path", "sha256", "verdict"),
                ("sample_path", "sample_sha256", "sample"),
                ("candidate_audit_path", "candidate_audit_sha256", "candidate audit"),
            ):
                source_path = receipt.get(path_field)
                if (
                    not isinstance(source_path, str)
                    or not Path(source_path).is_file()
                    or hash_file(Path(source_path)) != receipt.get(hash_field)
                ):
                    issues.append(f"Wave 3 family-gate {label} hash differs")
            audit = receipt.get("candidate_audit")
            audit_path_value = receipt.get("candidate_audit_path")
            audit_path = Path(audit_path_value) if isinstance(audit_path_value, str) else None
            external_audit: Mapping[str, Any] | None = None
            if audit_path is not None and audit_path.is_file():
                try:
                    external_audit = read_json_object(audit_path)
                except (OSError, ValueError, json.JSONDecodeError):
                    issues.append("Wave 3 family-gate candidate audit is malformed")
            if not isinstance(audit, Mapping) or audit != external_audit:
                issues.append("Wave 3 embedded candidate audit differs from its artifact")
                audit = {}
            candidate_receipt = receipt.get("candidate_run_receipt")
            if (
                not isinstance(candidate_receipt, Mapping)
                or audit.get("run_receipt") != candidate_receipt
                or audit.get("run_receipt_sha256") != hash_canonical(candidate_receipt)
            ):
                issues.append("Wave 3 candidate-run receipt hash differs")
            else:
                for text in _source_run_receipt_issues(candidate_receipt, staging_root, repo_root):
                    issues.append(f"Wave 3 candidate-run receipt: {text}")
                performance = candidate_receipt.get("performance")
                if (
                    not isinstance(performance, Mapping)
                    or performance.get("roots_considered") != 100
                    or audit.get("typed_candidates") != 100
                    or audit.get("run_id") != candidate_receipt.get("run_id")
                    or audit.get("failure_taxonomy") != candidate_receipt.get("failure_taxonomy")
                ):
                    issues.append("Wave 3 candidate-run audit totals differ")
                candidate_run_dir_value = candidate_receipt.get("run_dir")
                candidate_operation = receipt.get("operation_id")
                if not isinstance(candidate_run_dir_value, str) or not isinstance(
                    candidate_operation, str
                ):
                    issues.append("Wave 3 candidate-run identity is malformed")
                else:
                    candidate_run_dir = Path(candidate_run_dir_value)
                    try:
                        candidate_manifest = read_json_object(candidate_run_dir / "run.json")
                        candidate_status = read_json_object(candidate_run_dir / "status.json")
                        candidate_journal = read_jsonl(candidate_run_dir / "journal.jsonl")
                        candidate_records = read_jsonl(candidate_run_dir / "retained.jsonl")
                    except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
                        issues.append("Wave 3 candidate-run artifacts are malformed")
                    else:
                        roots_value = candidate_manifest.get("explicit_roots")
                        roots = (
                            [str(root) for root in roots_value]
                            if isinstance(roots_value, list)
                            else []
                        )
                        terminals = [
                            item for item in candidate_journal if item.get("kind") == "terminal"
                        ]
                        terminal_cells = [
                            (str(item.get("root")), str(item.get("operation_id")))
                            for item in terminals
                        ]
                        retained_cells = {
                            (str(item.get("root")), str(item.get("operation_id")))
                            for item in terminals
                            if item.get("status") == "retained"
                        }
                        record_cells = {
                            (
                                str((item.get("sidecar") or {}).get("root_name")),
                                str((item.get("sidecar") or {}).get("operation_id")),
                            )
                            for item in candidate_records
                            if isinstance(item.get("sidecar"), Mapping)
                        }
                        statuses: dict[str, int] = {}
                        reasons: dict[str, dict[str, int]] = {}
                        for item in terminals:
                            status_name = str(item.get("status", "missing"))
                            reason = str(item.get("reason", "")) or "none"
                            statuses[status_name] = statuses.get(status_name, 0) + 1
                            reason_counts = reasons.setdefault(status_name, {})
                            reason_counts[reason] = reason_counts.get(reason, 0) + 1
                        taxonomy = {
                            "terminal_statuses": dict(sorted(statuses.items())),
                            "terminal_reasons": {
                                status_name: dict(sorted(reason_counts.items()))
                                for status_name, reason_counts in sorted(reasons.items())
                            },
                        }
                        accounting = {
                            "root_count": len(roots),
                            "roots_sha256": hash_canonical(roots),
                            "terminal_count": len(terminals),
                            "terminal_cells_sha256": hash_canonical(sorted(terminal_cells)),
                            "retained_terminal_count": len(retained_cells),
                            "retained_row_count": len(candidate_records),
                            "status_retained_total": candidate_status.get("retained_total"),
                            "failure_taxonomy": taxonomy,
                        }
                        expected_cells = {(root, candidate_operation) for root in roots}
                        candidate_checks = audit.get("checks")
                        exact_candidate_evidence = (
                            len(roots) == 100
                            and len(set(roots)) == 100
                            and candidate_status.get("roots_considered") == 100
                            and candidate_manifest.get("operations") == [candidate_operation]
                            and len(terminals) == 100
                            and len(terminal_cells) == len(set(terminal_cells))
                            and set(terminal_cells) == expected_cells
                            and retained_cells == record_cells
                            and candidate_status.get("retained_total") == len(candidate_records)
                            and candidate_receipt.get("failure_taxonomy") == taxonomy
                            and audit.get("failure_taxonomy") == taxonomy
                            and audit.get("terminal_accounting") == accounting
                            and all(
                                not release_certificate_issues(item) for item in candidate_records
                            )
                            and isinstance(candidate_checks, Mapping)
                            and bool(candidate_checks)
                            and all(value is True for value in candidate_checks.values())
                        )
                        if not exact_candidate_evidence:
                            issues.append(
                                "Wave 3 candidate-run terminal/certificate accounting differs"
                            )
            fixture = receipt.get("fixture_receipt")
            if not isinstance(fixture, Mapping) or audit.get("fixture_receipt") != fixture:
                issues.append("Wave 3 fixture receipt differs from its candidate audit")
            else:
                fixture_path_value = fixture.get("path")
                fixture_path = (
                    Path(fixture_path_value) if isinstance(fixture_path_value, str) else None
                )
                if (
                    fixture_path is None
                    or not fixture_path.is_file()
                    or hash_file(fixture_path) != fixture.get("sha256")
                    or audit.get("fixture_report_path") != fixture_path_value
                    or audit.get("fixture_report_sha256") != fixture.get("sha256")
                ):
                    issues.append("Wave 3 fixture report hash differs")
                fixture_run_dir_value = fixture.get("run_dir")
                fixture_hashes = fixture.get("file_sha256")
                fixture_files_exact = False
                if isinstance(fixture_run_dir_value, str) and isinstance(fixture_hashes, Mapping):
                    fixture_run_dir = Path(fixture_run_dir_value)
                    fixture_files_exact = all(
                        (fixture_run_dir / filename).is_file()
                        and hash_file(fixture_run_dir / filename) == expected
                        for filename, expected in fixture_hashes.items()
                        if isinstance(filename, str) and isinstance(expected, str)
                    ) and set(fixture_hashes) == {
                        "run.json",
                        "status.json",
                        "journal.jsonl",
                        "retained.jsonl",
                    }
                fixture_checks = fixture.get("checks")
                if (
                    not fixture_files_exact
                    or not isinstance(fixture_checks, Mapping)
                    or not fixture_checks
                    or not all(value is True for value in fixture_checks.values())
                    or fixture.get("passed") is not True
                ):
                    issues.append("Wave 3 fixture receipt is not fully checked")
    else:
        issues.append("Wave 3 family-gate receipt list is malformed")

    mixed = document.get("mixed_200_gate")
    if isinstance(mixed, Mapping) and mixed.get("provided") is True:
        mixed_path = mixed.get("path")
        if (
            not isinstance(mixed_path, str)
            or not Path(mixed_path).is_file()
            or hash_file(Path(mixed_path)) != mixed.get("sha256")
        ):
            issues.append("Wave 3 mixed-200 gate report hash differs")
        mixed_runs = mixed.get("source_runs")
        if not isinstance(mixed_runs, list):
            issues.append("Wave 3 mixed-200 source receipt list is malformed")
        else:
            for receipt in mixed_runs:
                if not isinstance(receipt, Mapping):
                    issues.append("Wave 3 mixed-200 source receipt is malformed")
                    continue
                run_receipt = receipt.get("run_receipt")
                if not isinstance(run_receipt, Mapping) or receipt.get(
                    "run_receipt_sha256"
                ) != hash_canonical(run_receipt):
                    issues.append("Wave 3 mixed-200 run receipt hash differs")
                    continue
                for text in _source_run_receipt_issues(run_receipt, staging_root, repo_root):
                    issues.append(f"Wave 3 mixed-200 run receipt: {text}")
    if document.get("passed") is True:
        required = set(WAVE3_GATE_OPERATIONS)
        if (
            not isinstance(families, Mapping)
            or families.get("exact_family_set") is not True
            or set(families.get("useful_operations") or []).difference(required)
            or not isinstance(families.get("useful_family_count"), int)
            or families.get("useful_family_count", 0) < 3
            or not isinstance(mixed, Mapping)
            or mixed.get("passed") is not True
            or mixed.get("roots_considered") != 200
        ):
            issues.append("Wave 3 passing gate lacks exact family or mixed-200 evidence")
    return issues


def validate_view(
    *,
    repo_root: Path,
    staging_root: Path,
    run_id: str,
    compacted_dir: Path,
    retained_path: Path | None = None,
    retained_paths: Sequence[Path] = (),
    source_runs: Sequence[str | Mapping[str, Any]] = (),
) -> dict[str, Any]:
    issues: list[str] = []
    counts: dict[str, int] = {}

    def issue(text: str) -> None:
        if len(issues) < 200:
            issues.append(text)
        counts[text.split(":", 1)[0]] = counts.get(text.split(":", 1)[0], 0) + 1

    manifest_path = compacted_dir / "manifest.json"
    manifest = read_json_object(manifest_path)
    wave3_release = manifest.get("schema_version") == WAVE3_RELEASE_SCHEMA and str(
        manifest.get("release_id", "")
    ).startswith("wave3_release:")
    wave4_release = manifest.get("schema_version") == WAVE4_RELEASE_SCHEMA and str(
        manifest.get("release_id", "")
    ).startswith(WAVE4_RELEASE_ID_PREFIX)
    if wave4_release:
        return _validate_wave4_release(
            repo_root=repo_root,
            staging_root=staging_root,
            run_id=run_id,
            compacted_dir=compacted_dir,
            manifest=manifest,
            source_runs=source_runs,
        )
    if wave3_release:
        builder = manifest.get("release_builder")
        if not isinstance(builder, Mapping) or builder.get("dirty") is not False:
            issue("release_builder: Wave 3 release builder worktree was dirty")
        elif not git_commit_is_ancestor(repo_root, builder.get("commit")):
            issue("release_builder: Wave 3 release builder commit is not an ancestor")
    sources = list(retained_paths)
    source_keys_by_path: dict[Path, str] = {}
    retained_file_receipts: dict[str, Mapping[str, Any]] = {}
    if retained_path is not None:
        sources.append(retained_path)
    recorded_source_files = manifest.get("source_retained_files")
    recorded_sources = manifest.get("source_retained_paths")
    if isinstance(recorded_source_files, list) and recorded_source_files:
        sources = []
        source_file_paths: list[str] = []
        seen_source_keys: set[str] = set()
        for item in recorded_source_files:
            if not isinstance(item, Mapping):
                issue("source_retained_manifest: entry is not an object")
                continue
            path_value = item.get("path")
            source_key = item.get("source_key")
            if not isinstance(path_value, str) or not isinstance(source_key, str):
                issue("source_retained_manifest: entry lacks path/source_key")
                continue
            path = Path(path_value)
            if not path.is_absolute():
                path = staging_root / path
            sources.append(path)
            source_file_paths.append(str(path))
            source_keys_by_path[path.resolve()] = source_key
            retained_file_receipts[source_key] = item
            if source_key in seen_source_keys:
                issue(f"source_retained_manifest: repeated source key {source_key}")
            seen_source_keys.add(source_key)
            if not path.is_file():
                issue(f"source_retained_missing: {path}")
            elif hash_file(path) != item.get("sha256"):
                issue(f"source_retained_hash: {path}")
        if recorded_sources != source_file_paths:
            issue("source_retained_manifest: paths and hashed file receipts differ")
    elif isinstance(recorded_sources, list) and recorded_sources:
        # regenerated views name the exact retained files they were built from
        sources = [staging_root / str(item) for item in recorded_sources]
        for path in sources:
            if not path.is_file():
                issue(f"source_retained_missing: {path}")
    elif wave3_release:
        issue("source_retained_manifest: Wave 3 release lacks exact source retained files")

    snapshot_index: dict[tuple[str, str], dict[str, Any]] = {}
    snapshot_load_issues: list[str] = []
    if wave3_release:
        snapshot_index, snapshot_load_issues = load_wave3_cache_snapshot(
            compacted_dir, manifest.get("source_cache_snapshot")
        )
        for text in snapshot_load_issues:
            issue(f"source_cache_snapshot: {text}")
        for text in _wave3_gate_issues(
            repo_root=repo_root,
            staging_root=staging_root,
            release_dir=compacted_dir,
            manifest=manifest,
        ):
            issue(f"wave3_gate: {text}")
    # pair id -> stored (sidecar hash without view fields, model row) copies; hashing keeps
    # memory flat for views with hundreds of thousands of rows
    retained: dict[str, list[tuple[str, dict[str, Any], str, str | None]]] = {}
    for path in sources:
        if not path.is_file():
            continue
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                item = json.loads(line)
                retained.setdefault(_pair_id(item), []).append(
                    (
                        hash_canonical(_without_view_fields(item["sidecar"])),
                        dict(item["row"]),
                        hash_canonical(item),
                        source_keys_by_path.get(path.resolve()),
                    )
                )
    shard_dirs = sorted(compacted_dir.glob("shard-*"))
    total_rows = 0
    seen_pairs: set[str] = set()
    seen_keys: dict[str, str] = {}
    records: list[dict[str, Any]] = []
    aggregates = AggregateAccumulator()
    wave3_projects: dict[str, int] = {}
    for shard_dir in shard_dirs:
        shard_manifest = read_json_object(shard_dir / "manifest.json")
        rows_path = shard_dir / "rows.jsonl"
        sidecars_path = shard_dir / "sidecars.jsonl"
        if hash_file(rows_path) != shard_manifest.get("rows_sha256"):
            issue(f"shard_rows_hash: {shard_dir.name}")
        if hash_file(sidecars_path) != shard_manifest.get("sidecars_sha256"):
            issue(f"shard_sidecars_hash: {shard_dir.name}")
        rows = read_jsonl(rows_path)
        sidecars = read_jsonl(sidecars_path)
        if len(rows) != len(sidecars) or len(rows) != int(shard_manifest.get("row_count", -1)):
            issue(f"shard_row_count: {shard_dir.name}")
        for row, sidecar in zip(rows, sidecars, strict=False):
            total_rows += 1
            model_facing = set(row) == MODEL_FACING_ROW_FIELDS
            pair_id = str(sidecar.get("pair_id") if model_facing else row.get("pair_id"))
            if not model_facing and set(row) != ROW_FIELDS:
                issue(f"row_schema: {pair_id}")
            if not model_facing and sidecar.get("pair_id") != pair_id:
                issue(f"row_sidecar_join: {pair_id}")
                continue
            if pair_id in seen_pairs:
                issue(f"duplicate_pair_id: {pair_id}")
            seen_pairs.add(pair_id)
            operation = str(sidecar["operation_id"] if model_facing else row["operation_id"])
            row_root_id = str(sidecar["root_id"] if model_facing else row["root_id"])
            label = bool(row["label"])
            if sidecar.get("mechanism") != mechanism_of(operation):
                issue(f"mechanism_metadata: {pair_id}")
            expected = expected_label(operation, sidecar)
            if expected is None:
                expected = operation.startswith("P") and operation not in NEGATIVE_OPERATIONS
            if label != expected or bool(sidecar.get("label")) != label:
                issue(f"label_polarity: {pair_id}")
            if sidecar.get("root_id") != row_root_id or sidecar.get("operation_id") != operation:
                issue(f"sidecar_identity: {pair_id}")
            if wave3_release:
                for text in wave3_cache_reference_issues(
                    sidecar,
                    release_id=manifest.get("release_id"),
                    snapshot_index=snapshot_index,
                ):
                    issue(f"source_cache_binding: {pair_id}: {text}")
                release_source = (sidecar.get("release") or {}).get("source") or {}
                project_id = str(release_source.get("project_id", "missing"))
                wave3_projects[project_id] = wave3_projects.get(project_id, 0) + 1
            evidence_issue = _check_evidence(sidecar, operation)
            if evidence_issue:
                issue(f"{evidence_issue}: {pair_id}")
            repr_block = sidecar.get("repr") or {}
            reference = repr_block.get("reference") or {}
            candidate = repr_block.get("candidate") or {}
            reference_text = str(row["reference"])
            candidate_text = str(row["candidate"])
            if sidecar.get("orientation") == "swapped":
                reference_text, candidate_text = candidate_text, reference_text
            if (
                reference.get("goal_v1") != reference_text
                or candidate.get("goal_v1") != candidate_text
            ):
                issue(f"repr_text_mismatch: {pair_id}")
            if reference.get("rendered_goal_hash") != sha256_hex(reference_text.encode("utf-8")):
                issue(f"reference_render_hash: {pair_id}")
            if candidate.get("rendered_goal_hash") != sha256_hex(candidate_text.encode("utf-8")):
                issue(f"candidate_render_hash: {pair_id}")
            pair_payload: dict[str, Any] = {
                "root_id": row_root_id,
                "operation_id": operation,
                "reference_expr_hash": (reference.get("provenance") or {}).get("expr_hash"),
                "candidate_expr_hash": (candidate.get("provenance") or {}).get("expr_hash"),
            }
            if is_square_operation(operation):
                pair_payload = {
                    "root_id": row_root_id,
                    "operation_id": operation,
                    "row_kind": sidecar.get("row_kind"),
                    "reference_expr_hash": pair_payload["reference_expr_hash"],
                    "candidate_expr_hash": pair_payload["candidate_expr_hash"],
                }
            expected_pair = make_id(PAIR_PREFIX, pair_payload)
            if expected_pair != pair_id:
                issue(f"pair_id_recompute: {pair_id}")
            if row["reference"] == row["candidate"]:
                issue(f"self_pair: {pair_id}")
            for side in ("reference", "candidate"):
                violation = residue_violation(str(row[side]))
                if violation:
                    issue(f"residue_{violation}: {pair_id}")
            key = unordered_pair_key(
                str(reference.get("rendered_goal_hash")), str(candidate.get("rendered_goal_hash"))
            )
            if key in seen_keys:
                issue(f"duplicate_unordered_pair: {pair_id} vs {seen_keys[key]}")
            seen_keys[key] = pair_id
            candidates_for_pair = retained.get(pair_id, [])
            if not candidates_for_pair:
                issue(f"missing_from_retained: {pair_id}")
            else:
                stored_hash = hash_canonical(_without_view_fields(sidecar))
                # A pair may come from several source runs (overlapping roots); the
                # stored copy must equal one of them exactly (canonical hash equality).
                source_record = next(
                    (item for item in candidates_for_pair if item[0] == stored_hash), None
                )
                if wave3_release:
                    release = sidecar.get("release") or {}
                    release_source = release.get("source") or {}
                    source_record = next(
                        (
                            item
                            for item in candidates_for_pair
                            if item[0] == stored_hash
                            and item[2] == release.get("source_record_sha256")
                            and item[3] == release_source.get("source_key")
                        ),
                        None,
                    )
                if source_record is None:
                    issue(f"retained_record_mismatch: {pair_id}")
                    source_record = candidates_for_pair[0]
                source_row = source_record[1]
                if sidecar.get("orientation") != "swapped" and (
                    source_row["reference"] != row["reference"]
                    or source_row["candidate"] != row["candidate"]
                    or bool(source_row["label"]) != label
                ):
                    issue(f"retained_row_mismatch: {pair_id}")
                if sidecar.get("orientation") == "swapped" and (
                    source_row["reference"] != row["candidate"]
                    or source_row["candidate"] != row["reference"]
                ):
                    issue(f"orientation_swap_mismatch: {pair_id}")
            aggregates.add(sidecar)
            records.append({"row": row, "sidecar": _slim_for_provenance(sidecar)})
    if total_rows != int(manifest.get("retained_rows", -1)):
        issue("shard_conservation: total shard rows differ from manifest retained_rows")
    if wave3_release:
        derived_projects = dict(sorted(wave3_projects.items()))
        if manifest.get("projects") != derived_projects:
            issue("manifest_projects: project counts differ from release sidecars")
        if set(wave3_projects) != WAVE3_PROJECTS:
            issue("manifest_projects: released rows do not cover all three pinned projects")
    conservation = (
        int(manifest.get("input_records", 0))
        - sum(int(v) for v in (manifest.get("screen_rejections") or {}).values())
        - int(manifest.get("duplicates_removed", 0))
        - int(manifest.get("repeated_input_records_dropped", 0))
        - int(manifest.get("conflicting_rows_rejected", 0))
        - int(manifest.get("view_dropped", 0))
    )
    if conservation != int(manifest.get("retained_rows", -1)):
        issue("manifest_conservation: input - rejections - duplicates - conflicts - view drop")
    manifest_source_runs = manifest.get("source_runs")
    effective_source_runs: list[str | Mapping[str, Any]]
    if source_runs:
        effective_source_runs = list(source_runs)
    elif isinstance(manifest_source_runs, list) and manifest_source_runs:
        effective_source_runs = [
            item for item in manifest_source_runs if isinstance(item, str | Mapping)
        ]
        if len(effective_source_runs) != len(manifest_source_runs):
            issue("source_run_receipt: manifest contains malformed source-run entries")
    else:
        effective_source_runs = [run_id]
    if wave3_release and manifest.get("source_receipts_sha256") != hash_canonical(
        effective_source_runs
    ):
        issue("source_run_receipt: source receipt-set hash differs from the manifest")
    if wave3_release:
        embedded = [item for item in effective_source_runs if isinstance(item, Mapping)]
        receipt_by_key = {
            str(item.get("source_key")): item
            for item in embedded
            if isinstance(item.get("source_key"), str)
        }
        if not embedded or {str(item.get("project_id")) for item in embedded} != WAVE3_PROJECTS:
            issue("source_run_receipt: Wave 3 does not bind all three pinned projects")
        if len(receipt_by_key) != len(embedded):
            issue("source_run_receipt: source keys are missing or repeated")
        if set(receipt_by_key) != set(retained_file_receipts):
            issue("source_retained_manifest: source keys differ from source-run receipts")
        for source_key, retained_receipt in retained_file_receipts.items():
            source_receipt = receipt_by_key.get(source_key)
            if source_receipt is None:
                continue
            expected_path = str(Path(str(source_receipt.get("run_dir"))) / "retained.jsonl")
            input_hashes = source_receipt.get("input_sha256") or {}
            if (
                retained_receipt.get("run_id") != source_receipt.get("run_id")
                or retained_receipt.get("project_id") != source_receipt.get("project_id")
                or retained_receipt.get("path") != expected_path
                or retained_receipt.get("sha256") != input_hashes.get("retained")
            ):
                issue(f"source_retained_manifest: {source_key} differs from its source-run receipt")
    for source_run in effective_source_runs:
        for text in _source_run_receipt_issues(source_run, staging_root, repo_root):
            issue(f"source_run_receipt: {text}")
    aggregate_issues = manifest_aggregate_issues(manifest, derived=aggregates.result())
    for text in aggregate_issues:
        issue(f"manifest_aggregate: {text}")
    if wave3_release:
        provenance, provenance_snapshot_issues = derive_wave3_snapshot_provenance(
            records,
            repo_root=repo_root,
            release_dir=compacted_dir,
            snapshot=manifest.get("source_cache_snapshot"),
        )
        for text in provenance_snapshot_issues:
            if text not in snapshot_load_issues:
                issue(f"source_cache_snapshot: {text}")
    else:
        provenance = derive_provenance(
            records,
            repo_root=repo_root,
            cache_root=staging_root / "cache",
            release_dir=compacted_dir,
            allow_multiple_project_pins=bool(manifest.get("multiple_project_pins_allowed")),
        )
    if not provenance["consistent"]:
        for text in provenance["issues"]:
            issue(f"provenance: {text}")
    manifest_provenance = manifest.get("provenance")
    if not isinstance(manifest_provenance, dict):
        issue("manifest_provenance: manifest lacks sidecar-derived provenance")
    elif hash_canonical(manifest_provenance) != hash_canonical(provenance):
        issue(
            "manifest_provenance: recorded provenance object differs from the sidecar-derived one"
        )
    if manifest.get("orientation_rule") == "one_swapped_row_per_paired_root":
        per_root: dict[str, int] = {}
        swapped_total = 0
        for record in records:
            root = str(record["sidecar"].get("root_id"))
            swapped_here = record["sidecar"].get("orientation") == "swapped"
            per_root[root] = per_root.get(root, 0) + (1 if swapped_here else 0)
            swapped_total += 1 if swapped_here else 0
        if swapped_total * 2 != len(records) or any(count != 1 for count in per_root.values()):
            issue("orientation_rule: not exactly one swapped row per paired root")
    if manifest.get("finalized") is True:
        for shard_dir in shard_dirs:
            if read_json_object(shard_dir / "manifest.json").get("complete") is not True:
                issue(f"finalized_shard_incomplete: {shard_dir.name}")
    report = {
        "schema_version": 2 if wave3_release else 1,
        "run_id": run_id,
        "compacted_dir": "." if wave3_release else str(compacted_dir),
        "manifest_sha256": hash_file(manifest_path),
        "rows_checked": total_rows,
        "shards": len(shard_dirs),
        "source_retained_files_checked": len(sources),
        "source_cache_snapshot_records_checked": len(snapshot_index),
        "issue_counts": counts,
        "issues": issues,
        "provenance": provenance,
        "replay_semantics": REPLAY_SEMANTICS,
        "proof_check_time": "original_generation",
        "passed": not issues,
    }
    write_atomic(compacted_dir / "integrity_report.json", canonical_json_bytes(report) + b"\n")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    from leanfaith.sft1.sprint.runner import RunPaths, load_sprint_config

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=find_repo_root(Path.cwd()))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--view", default="raw")
    parser.add_argument("--compacted-dir", type=Path)
    parser.add_argument("--label", help="validate a multi-run view under compacted/<label>")
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    loaded = load_sprint_config(repo_root, args.config.resolve() if args.config else None)
    staging = Path(loaded.config.output.staging_root)
    if args.label:
        compacted = staging / "compacted" / args.label
        manifest = read_json_object(compacted / "manifest.json")
        raw_source_runs = manifest.get("source_runs", [])
        source_runs = (
            [item for item in raw_source_runs if isinstance(item, str | Mapping)]
            if isinstance(raw_source_runs, list)
            else []
        )
        retained_paths = []
        for source_run in source_runs:
            if isinstance(source_run, str):
                retained_paths.append(RunPaths(staging, source_run).retained)
            elif isinstance(source_run.get("run_dir"), str):
                retained_paths.append(Path(str(source_run["run_dir"])) / "retained.jsonl")
        report = validate_view(
            repo_root=repo_root,
            staging_root=staging,
            run_id=args.label,
            compacted_dir=compacted,
            retained_paths=retained_paths,
            source_runs=source_runs,
        )
        print(json.dumps({k: v for k, v in report.items() if k != "issues"}, indent=1))
        if report["issues"]:
            print("\n".join(report["issues"][:40]))
        return 0 if report["passed"] else 1
    if not args.run_id:
        parser.error("--run-id is required unless --label is given")
    paths = RunPaths(staging, args.run_id)
    compacted = args.compacted_dir or (
        paths.compacted
        if args.view == "raw"
        else paths.compacted.parent / f"{args.run_id}_{args.view}"
    )
    report = validate_view(
        repo_root=repo_root,
        staging_root=staging,
        run_id=args.run_id,
        compacted_dir=compacted,
        retained_path=paths.retained,
    )
    print(json.dumps({k: v for k, v in report.items() if k != "issues"}, indent=1))
    if report["issues"]:
        print("\n".join(report["issues"][:40]))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

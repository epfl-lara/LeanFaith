"""Model-facing views built from several certified runs without regeneration.

The shortcut-corrected core view (``core_v2``) is a deterministic matched
2x2 relation design plus polarity-paired extras:

* ``eq_relation``: P18 Eq→Eq positive and N25 Eq→Ne negative from the same
  equality root;
* ``ne_relation``: P_NE Ne→Ne positive and N25 Ne→Eq negative from the same
  disequality root;
* ``order``: every N32 negative with a same-root positive twin;
* ``guard``: N31 negatives capped at a small share of the core, each with a
  same-root positive twin (a redundant-guard positive when available).

Cells are equalized by stable root hash.  Orientation randomization is
applied to the stored rows (deterministic by row hash) and recorded in the
sidecar.  Every N31 row also lands in an auxiliary view.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from collections import deque
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.loading import LoadedConfig
from leanfaith.config.paths import find_repo_root
from leanfaith.schemas.ids import PAIR_PREFIX, make_id
from leanfaith.sft1.sprint.engine import (
    NEGATIVE_OPERATIONS,
    POSITIVE_OPERATIONS,
    mechanism_of,
)
from leanfaith.sft1.sprint.provenance import derive_provenance
from leanfaith.sft1.sprint.runner import (
    RunPaths,
    SprintConfig,
    _count_by,
    _mixed_run_receipt,
    ancestry_shards,
    group_by_ancestry,
    load_sprint_config,
    read_retained,
    release_certificate_issues,
    utc_now,
)
from leanfaith.sft1.sprint.screens import (
    GoldBlocklist,
    deduplicate,
    local_names,
    render_hash,
    residue_violation,
    unordered_pair_key,
)
from leanfaith.sft1.sprint.store import read_json_object, write_atomic

CORE_SALT = "sft1_sprint_core_v2"
# Twins are chosen by surface neutrality: a binder swap keeps the token
# multiset, side swaps keep it too, while hypothesis packing adds `∧` and a
# fresh name, so P23 is the last resort.
TWIN_PRIORITY_ORDER = (
    "P14_SWAP_INDEPENDENT_DATA_BINDERS_V1",
    "P15_SWAP_IFF_SIDES_V1",
    "P18_SYMMETRIZE_EQUALITY_V1",
    "P_NE_SYMMETRIZE_V1",
    "P_DROP_REDUNDANT_GUARD_PROOF_V1",
    "P23_CURRY_PROP_PAIR_V1",
)
TWIN_PRIORITY_GUARD = (
    "P_DROP_REDUNDANT_GUARD_PROOF_V1",
    "P14_SWAP_INDEPENDENT_DATA_BINDERS_V1",
    "P15_SWAP_IFF_SIDES_V1",
    "P18_SYMMETRIZE_EQUALITY_V1",
    "P_NE_SYMMETRIZE_V1",
    "P23_CURRY_PROP_PAIR_V1",
)


class ViewError(RuntimeError):
    """Fail-closed view construction error."""


def load_runs(loaded: LoadedConfig[SprintConfig], run_ids: Sequence[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for run_id in run_ids:
        paths = RunPaths(Path(loaded.config.output.staging_root), run_id)
        if not paths.retained.is_file():
            raise ViewError(f"run {run_id!r} has no retained records")
        for record in read_retained(paths.retained):
            record["source_run"] = run_id
            records.append(record)
    return records


def screen_records(
    records: Sequence[dict[str, Any]], gold: GoldBlocklist
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    kept: list[dict[str, Any]] = []
    rejections: dict[str, int] = {}
    for record in records:
        row = record["row"]
        reason = (
            residue_violation(str(row["reference"]))
            or residue_violation(str(row["candidate"]))
            or ("self_pair_text" if row["reference"] == row["candidate"] else None)
            or (
                "gold_blocklist"
                if gold.hit(str(row["reference"])) or gold.hit(str(row["candidate"]))
                else None
            )
        )
        if reason:
            rejections[reason] = rejections.get(reason, 0) + 1
            continue
        kept.append(record)
    return kept, rejections


def cell_of(record: Mapping[str, Any]) -> str | None:
    operation = str(record["operation_id"])
    detail = str((record["sidecar"].get("site") or {}).get("detail", ""))
    if operation == "P18_SYMMETRIZE_EQUALITY_V1":
        return "eq_pos"
    if operation == "N25_TOGGLE_EQ_NE_PROOF_V1" and detail == "eq_to_ne":
        return "eq_neg"
    if operation == "P_NE_SYMMETRIZE_V1":
        return "ne_pos"
    if operation == "N25_TOGGLE_EQ_NE_PROOF_V1" and detail == "ne_to_eq":
        return "ne_neg"
    return None


def root_rank(root_id: str) -> str:
    return hash_canonical([CORE_SALT, root_id])


def _pick(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return min(records, key=lambda item: str(item["row_hash"]))


def _twin(
    by_operation: Mapping[str, list[dict[str, Any]]], priority: Sequence[str]
) -> dict[str, Any] | None:
    for operation in priority:
        if by_operation.get(operation):
            return _pick(by_operation[operation])
    return None


def _store(record: dict[str, Any], family: str, cell: str) -> dict[str, Any]:
    """Copy a record into the core view with stored orientation randomization."""

    stored: dict[str, Any] = json.loads(json.dumps(record))
    row = stored["row"]
    swapped = int(str(record["row_hash"])[-1], 16) % 2 == 1
    if swapped:
        row["reference"], row["candidate"] = row["candidate"], row["reference"]
    stored["sidecar"]["orientation"] = "swapped" if swapped else "original"
    stored["sidecar"]["core_family"] = family
    stored["sidecar"]["core_cell"] = cell
    stored["core_family"] = family
    return stored


def build_core(
    records: Sequence[dict[str, Any]],
    *,
    n31_cap_fraction: float = 0.02,
    order_cap_fraction: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_root: dict[str, dict[str, list[dict[str, Any]]]] = {}
    ops_by_root: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for record in records:
        root = str(record["row"]["root_id"])
        cell = cell_of(record)
        if cell is not None:
            by_root.setdefault(root, {}).setdefault(cell, []).append(record)
        ops_by_root.setdefault(root, {}).setdefault(str(record["operation_id"]), []).append(record)
    eq_roots = sorted(
        (root for root, cells in by_root.items() if "eq_pos" in cells and "eq_neg" in cells),
        key=root_rank,
    )
    ne_roots = sorted(
        (root for root, cells in by_root.items() if "ne_pos" in cells and "ne_neg" in cells),
        key=root_rank,
    )
    k = min(len(eq_roots), len(ne_roots))
    core: list[dict[str, Any]] = []
    used_roots: set[str] = set()
    for root in eq_roots[:k]:
        core.append(_store(_pick(by_root[root]["eq_pos"]), "eq_relation", "eq_pos"))
        core.append(_store(_pick(by_root[root]["eq_neg"]), "eq_relation", "eq_neg"))
        used_roots.add(root)
    for root in ne_roots[:k]:
        core.append(_store(_pick(by_root[root]["ne_pos"]), "ne_relation", "ne_pos"))
        core.append(_store(_pick(by_root[root]["ne_neg"]), "ne_relation", "ne_neg"))
        used_roots.add(root)
    order_pairs = 0
    matched_rows = len(core)
    order_cap = None if order_cap_fraction is None else int(order_cap_fraction * matched_rows)
    for root in sorted(ops_by_root, key=root_rank):
        if order_cap is not None and order_pairs >= order_cap:
            break
        if root in used_roots or not ops_by_root[root].get("N32_SWAP_ROLE_ORDER_PROOF_V1"):
            continue
        twin = _twin(ops_by_root[root], TWIN_PRIORITY_ORDER)
        if twin is None:
            continue
        core.append(
            _store(_pick(ops_by_root[root]["N32_SWAP_ROLE_ORDER_PROOF_V1"]), "order", "lt_neg")
        )
        core.append(_store(twin, "order", f"lt_pos:{twin['operation_id']}"))
        used_roots.add(root)
        order_pairs += 1
    n31_cap = int(n31_cap_fraction * len(core))
    guard_pairs = 0
    for root in sorted(ops_by_root, key=root_rank):
        if guard_pairs >= n31_cap:
            break
        if root in used_roots or not ops_by_root[root].get("N31_DROP_REQUIRED_GUARD_PROOF_V1"):
            continue
        twin = _twin(ops_by_root[root], TWIN_PRIORITY_GUARD)
        if twin is None:
            continue
        core.append(
            _store(
                _pick(ops_by_root[root]["N31_DROP_REQUIRED_GUARD_PROOF_V1"]), "guard", "guard_neg"
            )
        )
        core.append(_store(twin, "guard", f"guard_pos:{twin['operation_id']}"))
        used_roots.add(root)
        guard_pairs += 1
    report = {
        "salt": CORE_SALT,
        "eq_roots_available": len(eq_roots),
        "ne_roots_available": len(ne_roots),
        "matched_roots_per_relation": k,
        "order_cap_fraction": order_cap_fraction,
        "order_cap_rows": order_cap,
        "order_pairs": order_pairs,
        "n31_cap_fraction": n31_cap_fraction,
        "n31_cap_rows": n31_cap,
        "guard_pairs": guard_pairs,
        "core_rows": len(core),
        "cells": _count_by([item["sidecar"] for item in core], "core_cell") if core else {},
        "families": _count_by(core, "core_family") if core else {},
        "orientation": _count_by([item["sidecar"] for item in core], "orientation") if core else {},
    }
    return core, report


def write_view(
    *,
    repo_root: Path,
    loaded: LoadedConfig[SprintConfig],
    label: str,
    records: Sequence[dict[str, Any]],
    source_runs: Sequence[str],
    extra: Mapping[str, Any],
    shard_size: int | None = None,
) -> dict[str, Any]:
    config = loaded.config
    out = Path(config.output.staging_root) / "compacted" / label
    out.mkdir(parents=True, exist_ok=True)
    kept = group_by_ancestry(records)
    size = shard_size or config.output.shard_size
    provenance = derive_provenance(
        kept, repo_root=repo_root, cache_root=Path(config.output.staging_root) / "cache"
    )
    if not provenance["consistent"]:
        raise ViewError("provenance inconsistent: " + "; ".join(provenance["issues"]))
    shard_manifests = []
    for number, shard in enumerate(ancestry_shards(kept, size), start=1):
        shard_dir = out / f"shard-{number:04d}"
        shard_dir.mkdir(parents=True, exist_ok=True)
        rows_bytes = b"".join(canonical_json_bytes(item["row"]) + b"\n" for item in shard)
        sidecar_bytes = b"".join(canonical_json_bytes(item["sidecar"]) + b"\n" for item in shard)
        write_atomic(shard_dir / "rows.jsonl", rows_bytes)
        write_atomic(shard_dir / "sidecars.jsonl", sidecar_bytes)
        manifest = {
            "schema_version": 1,
            "view": label,
            "shard": number,
            "row_count": len(shard),
            "complete": len(shard) >= size,
            "labels": {
                "positive": sum(1 for item in shard if item["label"]),
                "negative": sum(1 for item in shard if not item["label"]),
            },
            "operations": _count_by(shard, "operation_id"),
            "roots": len({item["row"]["root_id"] for item in shard}),
            "rows_sha256": sha256_hex(rows_bytes),
            "sidecars_sha256": sha256_hex(sidecar_bytes),
            "first_row_hash": shard[0]["row_hash"],
            "last_row_hash": shard[-1]["row_hash"],
            "engine_source_sha256_set": sorted(
                {str(item["sidecar"]["engine"]["source_sha256"]) for item in shard}
            ),
        }
        write_atomic(shard_dir / "manifest.json", canonical_json_bytes(manifest) + b"\n")
        shard_manifests.append(manifest)
    manifest = {
        "schema_version": 1,
        "sprint_id": config.sprint_id,
        "run_id": label,
        "view": label,
        "source_runs": list(source_runs),
        "compacted_at": utc_now(),
        "retained_rows": len(kept),
        "labels": {
            "positive": sum(1 for item in kept if item["label"]),
            "negative": sum(1 for item in kept if not item["label"]),
        },
        "operations": _count_by(kept, "operation_id"),
        "mechanisms": _count_by(kept, "mechanism"),
        "roots": len({item["row"]["root_id"] for item in kept}),
        "shard_size": size,
        "shards": shard_manifests,
        "config_semantic_hash": loaded.config_hash,
        "provenance": provenance,
        "proof_check_time": "original_generation",
        "replay_semantics": "journal_and_cache_replay_of_stored_terminals_no_fresh_kernel_replay",
        "gold_blocklist_sha256": extra.get("gold_blocklist_sha256"),
        **{k: v for k, v in extra.items() if k != "gold_blocklist_sha256"},
    }
    write_atomic(out / "manifest.json", canonical_json_bytes(manifest) + b"\n")
    return manifest


def build_views(
    repo_root: Path,
    loaded: LoadedConfig[SprintConfig],
    *,
    run_ids: Sequence[str],
    label: str = "core_v2",
    n31_cap_fraction: float = 0.02,
    order_cap_fraction: float | None = None,
    minimum_negative_pairs: int = 100,
) -> dict[str, Any]:
    from leanfaith.sft1.sprint import shortcut

    config = loaded.config
    gold = GoldBlocklist.load(
        repo_root / config.screens.gold_blocklist_path,
        expected_sha256=config.screens.gold_blocklist_sha256,
    )
    records = load_runs(loaded, run_ids)
    screened, rejections = screen_records(records, gold)
    outcome = deduplicate(screened)
    conflicting_rows = sum(
        1 for record in screened if str(record["unordered_pair_key"]) in set(outcome.conflict_keys)
    )
    joined_stats = {
        "input_records": len(records),
        "input_by_run": _count_by(records, "source_run"),
        "screen_rejections": rejections,
        "duplicates_removed": outcome.duplicate_count,
        "conflicting_classes_rejected": outcome.conflict_count,
        "conflicting_rows_rejected": conflicting_rows,
        "deduplicated_records": len(outcome.kept),
        "artifact_status": "candidate_model_facing_view",
        "gold_blocklist_sha256": gold.sha256,
    }
    core, core_report = build_core(
        outcome.kept, n31_cap_fraction=n31_cap_fraction, order_cap_fraction=order_cap_fraction
    )
    core_manifest = write_view(
        repo_root=repo_root,
        loaded=loaded,
        label=label,
        records=core,
        source_runs=run_ids,
        extra={
            **joined_stats,
            "core_report": core_report,
            "view_dropped": len(outcome.kept) - len(core),
        },
    )
    aux = [
        json.loads(json.dumps(item))
        for item in outcome.kept
        if item["operation_id"] == "N31_DROP_REQUIRED_GUARD_PROOF_V1"
    ]
    for item in aux:
        item["sidecar"]["orientation"] = "original"
    aux_manifest = write_view(
        repo_root=repo_root,
        loaded=loaded,
        label=f"aux_n31_{label}",
        records=aux,
        source_runs=run_ids,
        extra={
            **joined_stats,
            "artifact_status": "auxiliary_view_not_model_facing",
            "view_dropped": len(outcome.kept) - len(aux),
        },
    )
    screens = shortcut.run_screens_v2(core) if core else {"passed": False, "screens": []}
    operations = core_manifest["operations"]
    negatives = {op: n for op, n in operations.items() if op in NEGATIVE_OPERATIONS}
    useful = [op for op, n in negatives.items() if n >= minimum_negative_pairs]
    unchecked = 0
    for item in core:
        evidence = item["sidecar"].get("evidence") or {}
        op = str(item["operation_id"])
        check = (
            (evidence.get("equivalence_proof") or {}).get("check")
            if op in POSITIVE_OPERATIONS
            else (evidence.get("refutation") or {}).get("check")
        )
        if not check or not check.get("meta_checked") or not check.get("kernel_checked"):
            unchecked += 1
    checks = {
        "core_nonempty": len(core) > 0,
        "all_rows_kernel_and_meta_checked_at_generation": unchecked == 0,
        "zero_conflicting_pairs": outcome.conflict_count == 0 and conflicting_rows == 0,
        "labels_balanced": core_manifest["labels"]["positive"]
        == core_manifest["labels"]["negative"],
        "two_useful_negative_mechanisms": len(useful) >= 2,
        "shortcut_screens": bool(screens["passed"]),
    }
    report = {
        "schema_version": 1,
        "view": label,
        "generated_at": utc_now(),
        "source_runs": list(run_ids),
        "joined": joined_stats,
        "core": {k: v for k, v in core_manifest.items() if k not in {"shards", "provenance"}},
        "core_report": core_report,
        "aux_n31_rows": aux_manifest["retained_rows"],
        "useful_negative_mechanisms": sorted(useful),
        "unchecked_rows": unchecked,
        "shortcut": screens,
        "proof_check_time": "original_generation",
        "replay_semantics": "journal_and_cache_replay_of_stored_terminals_no_fresh_kernel_replay",
        "checks": checks,
        "passed": all(checks.values()),
    }
    out = Path(config.output.staging_root) / "compacted" / label
    write_atomic(out / "release_report.json", canonical_json_bytes(report) + b"\n")
    return report


# ------------------------------------------------ additive Wave 3 multi-project release

WAVE3_RELEASE_SCHEMA = 3
WAVE3_RELEASE_SALT = "sft1_wave3_natural_core_release_v1"
WAVE3_PROJECTS = frozenset({"mathlib", "physlib", "cslib"})
WAVE3_GATE_SCHEMA = 1
WAVE3_GATE_OPERATIONS = (
    "N26_INCREMENT_BOUND_PROOF_V1",
    "N29_SWAP_WITNESS_DEPENDENCY_PROOF_V1",
    "N30_ADD_UNJUSTIFIED_UNIQUENESS_PROOF_V1",
    "N31_DROP_REQUIRED_GUARD_PROOF_V1",
    "N32_SWAP_ROLE_ORDER_PROOF_V1",
)
MODEL_ROW_FIELDS = frozenset({"reference", "candidate", "label"})
SOURCE_ROW_FIELDS = frozenset(
    {"pair_id", "root_id", "reference", "candidate", "label", "operation_id"}
)
N19_MARKERS = frozenset({"N19", "SQ19", "SQUARE_N19_CURRICULUM_V1"})
N25_OPERATION = "N25_TOGGLE_EQ_NE_PROOF_V1"
_RELATION_CHARS = "↔≠=≤<≥>∣∈⊆∧∨→"  # noqa: RUF001
_CONNECTIVES = ("∃!", "↔", "→", "∧", "∨", "¬", "∀", "∃")  # noqa: RUF001
_REQUIRED_RUN_FILES = (
    "run.json",
    "status.json",
    "journal.jsonl",
    "retained.jsonl",
    "replay_report.json",
)
_RECEIPT_HASH_NAMES = {
    "run.json": "manifest",
    "status.json": "status",
    "journal.jsonl": "journal",
    "retained.jsonl": "retained",
    "replay_report.json": "replay",
}


def _is_n19(operation: str, mechanism: str) -> bool:
    return (
        operation.startswith("N19_")
        or operation.startswith("SQUARE_N19_")
        or mechanism in N19_MARKERS
    )


def _git_identity(repo_root: Path) -> dict[str, object]:
    def run(*arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo_root), *arguments],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    return {
        "commit": run("rev-parse", "HEAD"),
        "dirty": bool(run("status", "--porcelain")),
        "views_source_sha256": hash_file(Path(__file__)),
    }


def _strict_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ViewError(f"cannot read {path}: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ViewError(f"malformed JSON at {path}:{line_number}") from exc
        if not isinstance(value, dict):
            raise ViewError(f"JSONL row is not an object at {path}:{line_number}")
        records.append(value)
    return records


def _run_file_hashes(run_dir: Path) -> dict[str, str]:
    missing = [name for name in _REQUIRED_RUN_FILES if not (run_dir / name).is_file()]
    if missing:
        raise ViewError(f"run {run_dir} is missing required files: {', '.join(missing)}")
    return {name: hash_file(run_dir / name) for name in _REQUIRED_RUN_FILES}


def _terminal_taxonomy(journal: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    statuses: dict[str, int] = {}
    reasons: dict[str, dict[str, int]] = {}
    for record in journal:
        if record.get("kind") != "terminal":
            continue
        status = str(record.get("status", "missing"))
        reason = str(record.get("reason", "")) or "none"
        statuses[status] = statuses.get(status, 0) + 1
        reasons.setdefault(status, {})[reason] = reasons.setdefault(status, {}).get(reason, 0) + 1
    return {
        "terminal_statuses": dict(sorted(statuses.items())),
        "terminal_reasons": {
            status: dict(sorted(values.items())) for status, values in sorted(reasons.items())
        },
    }


def _load_completed_run(
    run_dir: Path, *, gold_sha256: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load a completed run while proving its files did not change underfoot."""

    resolved = run_dir.resolve()
    before = _run_file_hashes(resolved)
    try:
        receipt, records = _mixed_run_receipt(resolved)
    except RuntimeError as exc:
        raise ViewError(f"run {resolved} failed durable receipt validation: {exc}") from exc
    after = _run_file_hashes(resolved)
    receipt_hashes = cast(Mapping[str, Any], receipt.get("input_sha256"))
    normalized_receipt_hashes = {
        filename: receipt_hashes.get(receipt_name)
        for filename, receipt_name in _RECEIPT_HASH_NAMES.items()
    }
    if before != after or normalized_receipt_hashes != after:
        raise ViewError(f"run {resolved} changed while its release receipt was being read")

    terminal_counts = cast(Mapping[str, Any], receipt["terminal_counts"])
    checks = {
        "manifest_and_root_selection_bound": bool(receipt["explicit_roots_hash_matches"]),
        "terminal_cells_complete": all(
            int(terminal_counts[field]) == 0
            for field in ("missing", "duplicate", "unexpected", "invalid_status")
        )
        and int(terminal_counts["observed"]) == int(terminal_counts["expected"]),
        "generation_status_complete": bool(receipt["generation_status_complete"]),
        "retained_terminal_join_complete": int(receipt["record_issues"]) == 0
        and int(receipt["record_join_defects"]) == 0,
        "replay_zero_call": bool(receipt["replay_zero_call"]),
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ViewError(f"run {resolved} is not release-authorized: {', '.join(failed)}")

    manifest = read_json_object(resolved / "run.json")
    from leanfaith.sft1.sprint.integrity import git_commit_is_ancestor

    if manifest.get("implementation_dirty") is not False:
        raise ViewError(f"run {resolved} was generated from a dirty worktree")
    if not git_commit_is_ancestor(
        find_repo_root(Path(__file__)), manifest.get("implementation_commit")
    ):
        raise ViewError(f"run {resolved} generator commit is not an ancestor of this release")
    if manifest.get("gold_blocklist_sha256") != gold_sha256:
        raise ViewError(f"run {resolved} used a different gold blocklist")
    status = read_json_object(resolved / "status.json")
    journal = _strict_jsonl(resolved / "journal.jsonl")
    retained_terminals = {
        (str(item.get("root")), str(item.get("operation_id"))): item
        for item in journal
        if item.get("kind") == "terminal" and item.get("status") == "retained"
    }
    for record in records:
        sidecar = cast(Mapping[str, Any], record.get("sidecar"))
        cell = (str(sidecar.get("root_name")), str(sidecar.get("operation_id")))
        terminal = retained_terminals.get(cell)
        if terminal is None:
            raise ViewError(f"retained row {cell!r} lacks an authorizing terminal in {resolved}")
        for field, expected in (
            ("pair_id", sidecar.get("pair_id")),
            ("row_hash", record.get("row_hash")),
            ("unordered_pair_key", record.get("unordered_pair_key")),
        ):
            if not isinstance(terminal.get(field), str) or terminal.get(field) != expected:
                raise ViewError(f"retained terminal {cell!r} does not bind {field} in {resolved}")

    project = cast(Mapping[str, Any], manifest["project"])
    run_id = str(manifest["run_id"])
    source_key = hash_canonical([project["project_id"], project["project_revision"], run_id, after])
    enriched = {
        **receipt,
        "run_dir": str(resolved),
        "source_key": source_key,
        "sprint_id": manifest.get("sprint_id"),
        "config_semantic_hash": manifest.get("config_semantic_hash"),
        "engine": manifest.get("engine"),
        "gold_blocklist_sha256": manifest.get("gold_blocklist_sha256"),
        "implementation_commit": manifest.get("implementation_commit"),
        "implementation_dirty": manifest.get("implementation_dirty"),
        "runner_source_sha256": manifest.get("runner_source_sha256"),
        "checks": checks,
        "performance": {
            field: status.get(field)
            for field in (
                "roots_considered",
                "roots_lean",
                "roots_cache",
                "lean_requests",
                "lean_elapsed_ms",
                "wall_seconds",
                "peak_process_tree_rss_bytes",
            )
        },
        "failure_taxonomy": _terminal_taxonomy(journal),
    }
    return enriched, records


def _goal_target(text: str) -> str:
    return text.rsplit("⊢", 1)[-1].strip()


def _target_relation(text: str) -> str:
    target = _goal_target(text)
    if target.startswith("¬"):
        return "¬"
    depth = 0
    for character in target:
        if character in "([{":
            depth += 1
        elif character in ")]}":
            depth = max(0, depth - 1)
        elif depth == 0 and character in _RELATION_CHARS:
            return character
    return "none"


def _binder_count(text: str) -> int:
    target = _goal_target(text)
    return len(local_names(text)) + target.count("∀") + target.count("∃")


def _connective_signature(text: str) -> tuple[int, ...]:
    target = _goal_target(text)
    return tuple(target.count(token) for token in _CONNECTIVES)


def wave3_pair_delta(record: Mapping[str, Any]) -> dict[str, Any]:
    row = cast(Mapping[str, Any], record["row"])
    reference = str(row["reference"])
    candidate = str(row["candidate"])
    reference_relation = _target_relation(reference)
    candidate_relation = _target_relation(candidate)
    reference_binders = _binder_count(reference)
    candidate_binders = _binder_count(candidate)
    reference_connectives = _connective_signature(reference)
    candidate_connectives = _connective_signature(candidate)
    relation_agrees = reference_relation == candidate_relation
    target_equal = _goal_target(reference) == _goal_target(candidate)
    binder_equal = reference_binders == candidate_binders
    connective_equal = reference_connectives == candidate_connectives
    cell = "|".join(
        (
            "relation_same" if relation_agrees else "relation_changed",
            "target_same" if target_equal else "target_changed",
            "binders_same" if binder_equal else "binders_changed",
            "connectives_same" if connective_equal else "connectives_changed",
        )
    )
    return {
        "cell": cell,
        "relation": {
            "reference": reference_relation,
            "candidate": candidate_relation,
            "agrees": relation_agrees,
        },
        "target_text_equal": target_equal,
        "binders": {
            "reference": reference_binders,
            "candidate": candidate_binders,
            "changed": not binder_equal,
        },
        "connectives": {
            "tokens": list(_CONNECTIVES),
            "reference": list(reference_connectives),
            "candidate": list(candidate_connectives),
            "changed": not connective_equal,
        },
    }


def _source_record_issue(
    record: Mapping[str, Any], receipt: Mapping[str, Any], gold: GoldBlocklist
) -> str | None:
    row = record.get("row")
    sidecar = record.get("sidecar")
    if not isinstance(row, Mapping) or not isinstance(sidecar, Mapping):
        raise ViewError("retained record lacks row or sidecar object")
    if set(row) != SOURCE_ROW_FIELDS:
        raise ViewError(
            f"source row has fields {sorted(row)} instead of {sorted(SOURCE_ROW_FIELDS)}"
        )

    operation = str(row.get("operation_id", ""))
    label = row.get("label")
    pair_id = row.get("pair_id")
    root_id = row.get("root_id")
    reference = row.get("reference")
    candidate = row.get("candidate")
    if (
        not isinstance(pair_id, str)
        or not pair_id.startswith("pair:")
        or not isinstance(root_id, str)
        or not root_id.startswith("root:")
        or not isinstance(reference, str)
        or not isinstance(candidate, str)
        or type(label) is not bool
    ):
        raise ViewError(f"malformed retained row identity for {pair_id!r}")
    if reference == candidate:
        raise ViewError(f"self pair reached release input: {pair_id}")
    if _is_n19(operation, str(sidecar.get("mechanism", ""))):
        raise ViewError(f"forbidden N19 record reached Wave 3 release: {pair_id}")
    try:
        mechanism = mechanism_of(operation)
    except RuntimeError as exc:
        raise ViewError(f"unknown release operation {operation!r}") from exc
    expected_label = operation in POSITIVE_OPERATIONS
    if operation not in POSITIVE_OPERATIONS | NEGATIVE_OPERATIONS or label is not expected_label:
        raise ViewError(f"operation polarity mismatch for {pair_id}")
    identities = {
        "pair_id": pair_id,
        "root_id": root_id,
        "operation_id": operation,
        "label": label,
    }
    for field, expected in identities.items():
        if sidecar.get(field) != expected:
            raise ViewError(f"row/sidecar {field} mismatch for {pair_id}")
    if sidecar.get("mechanism") != mechanism:
        raise ViewError(f"mechanism mismatch for {pair_id}")
    for field, expected in (
        ("operation_id", operation),
        ("label", label),
        ("root_name", sidecar.get("root_name")),
    ):
        if record.get(field) != expected:
            raise ViewError(f"top-level {field} mismatch for {pair_id}")
    row_hash = record.get("row_hash")
    pair_key = record.get("unordered_pair_key")
    if not isinstance(row_hash, str) or len(row_hash) != 64:
        raise ViewError(f"malformed row hash for {pair_id}")
    if not isinstance(pair_key, str) or len(pair_key) != 64:
        raise ViewError(f"malformed unordered-pair key for {pair_id}")

    project = sidecar.get("project")
    if not isinstance(project, Mapping) or (
        project.get("project_id") != receipt["project_id"]
        or project.get("project_revision") != receipt["project_revision"]
    ):
        raise ViewError(f"project identity mismatch for {pair_id}")
    if sidecar.get("engine") != receipt.get("engine"):
        raise ViewError(f"engine identity mismatch for {pair_id}")

    repr_block = sidecar.get("repr")
    if not isinstance(repr_block, Mapping):
        raise ViewError(f"representation evidence missing for {pair_id}")
    reference_repr = repr_block.get("reference")
    candidate_repr = repr_block.get("candidate")
    if not isinstance(reference_repr, Mapping) or not isinstance(candidate_repr, Mapping):
        raise ViewError(f"endpoint representation evidence missing for {pair_id}")
    if reference_repr.get("goal_v1") != reference or candidate_repr.get("goal_v1") != candidate:
        raise ViewError(f"endpoint text does not match representation evidence for {pair_id}")
    reference_hash = render_hash(reference)
    candidate_hash = render_hash(candidate)
    if (
        reference_repr.get("rendered_goal_hash") != reference_hash
        or candidate_repr.get("rendered_goal_hash") != candidate_hash
        or unordered_pair_key(reference_hash, candidate_hash) != pair_key
    ):
        raise ViewError(f"render or unordered-pair hash mismatch for {pair_id}")
    reference_provenance = reference_repr.get("provenance")
    candidate_provenance = candidate_repr.get("provenance")
    if not isinstance(reference_provenance, Mapping) or not isinstance(
        candidate_provenance, Mapping
    ):
        raise ViewError(f"endpoint expression hashes missing for {pair_id}")
    expected_pair_id = make_id(
        PAIR_PREFIX,
        {
            "root_id": root_id,
            "operation_id": operation,
            "reference_expr_hash": reference_provenance.get("expr_hash"),
            "candidate_expr_hash": candidate_provenance.get("expr_hash"),
        },
    )
    if expected_pair_id != pair_id:
        raise ViewError(f"pair ID does not recompute for {pair_id}")
    evidence = sidecar.get("evidence")
    if sidecar.get("evidence_hash") != hash_canonical(evidence):
        raise ViewError(f"evidence hash mismatch for {pair_id}")
    certificate_issues = release_certificate_issues(record)
    if certificate_issues:
        raise ViewError(f"certificate defect for {pair_id}: {', '.join(certificate_issues)}")

    violation = residue_violation(reference) or residue_violation(candidate)
    if violation is not None:
        return violation
    if gold.hit(reference) or gold.hit(candidate):
        return "gold_blocklist"
    return None


def _record_pair_id(record: Mapping[str, Any]) -> str:
    return str(cast(Mapping[str, Any], record["row"])["pair_id"])


def _stable_interleave(
    records: Sequence[dict[str, Any]], *, group_fields: Sequence[str], salt: str
) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for record in records:
        source = cast(Mapping[str, Any], record["_release_source"])
        delta = cast(Mapping[str, Any], record["_pair_delta"])
        values = {
            "project": str(source["project_id"]),
            "operation": str(record["operation_id"]),
            "surface_cell": str(delta["cell"]),
        }
        key = tuple(values[field] for field in group_fields)
        buckets.setdefault(key, []).append(record)
    queues: list[tuple[tuple[str, ...], deque[dict[str, Any]]]] = []
    for key in sorted(buckets, key=lambda item: hash_canonical([salt, item])):
        ordered = sorted(
            buckets[key],
            key=lambda item: hash_canonical([salt, key, _record_pair_id(item), item["row_hash"]]),
        )
        queues.append((key, deque(ordered)))
    output: list[dict[str, Any]] = []
    while queues:
        remaining: list[tuple[tuple[str, ...], deque[dict[str, Any]]]] = []
        for key, queue in queues:
            output.append(queue.popleft())
            if queue:
                remaining.append((key, queue))
        queues = remaining
    return output


def _collapse_exact_pair_ids(
    records: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    by_pair: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_pair.setdefault(_record_pair_id(record), []).append(record)
    kept: list[dict[str, Any]] = []
    repeats = 0
    for pair_id in sorted(by_pair):
        members = by_pair[pair_id]
        source_hashes = {str(member["_source_record_sha256"]) for member in members}
        if len(source_hashes) != 1:
            raise ViewError(f"pair ID collision with differing retained evidence: {pair_id}")
        winner = min(
            members,
            key=lambda item: str(cast(Mapping[str, Any], item["_release_source"])["source_key"]),
        )
        kept.append(winner)
        repeats += len(members) - 1
    row_hash_to_pair: dict[str, str] = {}
    for record in kept:
        row_hash = str(record["row_hash"])
        pair_id = _record_pair_id(record)
        other = row_hash_to_pair.setdefault(row_hash, pair_id)
        if other != pair_id:
            raise ViewError(f"row hash collision between {other} and {pair_id}")
    return kept, repeats


def _balanced_wave3_selection(
    records: Sequence[dict[str, Any]],
    *,
    maximum_rows: int | None,
    n25_cap_fraction: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not 0.0 <= n25_cap_fraction <= 0.25:
        raise ViewError("Wave 3 N25 cap must be between 0 and 0.25")
    if maximum_rows is not None and maximum_rows < 2:
        raise ViewError("maximum_rows must be at least two")
    by_cell: dict[str, dict[bool, list[dict[str, Any]]]] = {}
    for record in records:
        cell = str(cast(Mapping[str, Any], record["_pair_delta"])["cell"])
        by_cell.setdefault(cell, {True: [], False: []})[bool(record["label"])].append(record)

    units: list[tuple[dict[str, Any], dict[str, Any]]] = []
    cell_availability: dict[str, dict[str, int]] = {}
    for cell in sorted(by_cell):
        positives = _stable_interleave(
            by_cell[cell][True],
            group_fields=("project", "operation"),
            salt=f"{WAVE3_RELEASE_SALT}:positive:{cell}",
        )
        negatives = _stable_interleave(
            by_cell[cell][False],
            group_fields=("project", "operation"),
            salt=f"{WAVE3_RELEASE_SALT}:negative:{cell}",
        )
        matched = min(len(positives), len(negatives))
        units.extend(zip(positives[:matched], negatives[:matched], strict=True))
        cell_availability[cell] = {
            "positive": len(positives),
            "negative": len(negatives),
            "matched_per_label": matched,
        }

    n25_units = [unit for unit in units if unit[1]["operation_id"] == N25_OPERATION]
    other_units = [unit for unit in units if unit[1]["operation_id"] != N25_OPERATION]
    desired = len(units)
    if maximum_rows is not None:
        desired = min(desired, maximum_rows // 2)
    if n25_cap_fraction < 0.5:
        maximum_supported = math.floor(len(other_units) / (1.0 - 2.0 * n25_cap_fraction))
        desired = min(desired, maximum_supported)
    n25_ceiling = math.floor(2 * desired * n25_cap_fraction + 1e-12)
    minimum_n25 = max(0, desired - len(other_units))
    proportional_n25 = round(desired * len(n25_units) / len(units)) if units else 0
    selected_n25 = max(minimum_n25, min(len(n25_units), n25_ceiling, proportional_n25))
    selected_other = desired - selected_n25

    def unit_order(
        candidates: Sequence[tuple[dict[str, Any], dict[str, Any]]], salt: str
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        buckets: dict[tuple[str, ...], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
        for unit in candidates:
            key = (
                str(cast(Mapping[str, Any], unit[0]["_pair_delta"])["cell"]),
                str(cast(Mapping[str, Any], unit[0]["_release_source"])["project_id"]),
                str(unit[0]["operation_id"]),
                str(cast(Mapping[str, Any], unit[1]["_release_source"])["project_id"]),
                str(unit[1]["operation_id"]),
            )
            buckets.setdefault(key, []).append(unit)
        queues = [
            deque(
                sorted(
                    buckets[key],
                    key=lambda unit: hash_canonical(
                        [salt, key, _record_pair_id(unit[0]), _record_pair_id(unit[1])]
                    ),
                )
            )
            for key in sorted(buckets, key=lambda item: hash_canonical([salt, item]))
        ]
        ordered: list[tuple[dict[str, Any], dict[str, Any]]] = []
        while queues:
            remaining = []
            for queue in queues:
                ordered.append(queue.popleft())
                if queue:
                    remaining.append(queue)
            queues = remaining
        return ordered

    chosen_units = (
        unit_order(other_units, f"{WAVE3_RELEASE_SALT}:unit:other")[:selected_other]
        + unit_order(n25_units, f"{WAVE3_RELEASE_SALT}:unit:n25")[:selected_n25]
    )
    chosen_units.sort(
        key=lambda unit: hash_canonical(
            [WAVE3_RELEASE_SALT, "unit-final", _record_pair_id(unit[0]), _record_pair_id(unit[1])]
        )
    )
    selected = [record for unit in chosen_units for record in unit]
    selected_cells: dict[str, dict[str, int]] = {}
    for record in selected:
        cell = str(cast(Mapping[str, Any], record["_pair_delta"])["cell"])
        polarity = "positive" if record["label"] else "negative"
        selected_cells.setdefault(cell, {"positive": 0, "negative": 0})[polarity] += 1
    report = {
        "policy": "joint_pair_delta_cell_match_then_stable_operation_source_interleave_v1",
        "salt": WAVE3_RELEASE_SALT,
        "maximum_rows": maximum_rows,
        "n25_cap_fraction": n25_cap_fraction,
        "input_rows": len(records),
        "pair_delta_matched_units_available": len(units),
        "selected_units": len(chosen_units),
        "selected_rows": len(selected),
        "n25_units_available": len(n25_units),
        "n25_units_selected": selected_n25,
        "other_negative_units_available": len(other_units),
        "other_negative_units_selected": selected_other,
        "pair_delta_cells_available": cell_availability,
        "pair_delta_cells_selected": selected_cells,
        "rows_dropped_for_pair_delta_or_capacity": len(records) - len(selected),
    }
    return selected, report


def _release_shards(
    records: Sequence[dict[str, Any]], shard_size: int
) -> list[list[dict[str, Any]]]:
    ordered = sorted(
        records,
        key=lambda item: (
            hash_canonical(str(cast(Mapping[str, Any], item["sidecar"])["root_id"])),
            str(item["row_hash"]),
        ),
    )
    shards: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_root: str | None = None
    for record in ordered:
        root_id = str(cast(Mapping[str, Any], record["sidecar"])["root_id"])
        if current and len(current) >= shard_size and root_id != current_root:
            shards.append(current)
            current = []
        current.append(record)
        current_root = root_id
    if current:
        shards.append(current)
    return shards


def _count_nested(records: Sequence[Mapping[str, Any]], *fields: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        value: Any = record
        for field in fields:
            if not isinstance(value, Mapping):
                value = "missing"
                break
            value = value.get(field, "missing")
        key = str(value)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _fixture_report_receipt(path: Path, *, operation_id: str, gold_sha256: str) -> dict[str, Any]:
    """Bind one live-retained and one typed fail-closed fixture to runner artifacts."""

    resolved = path.resolve()
    report = read_json_object(resolved)
    run_dir = resolved.parent
    required = ("run.json", "status.json", "journal.jsonl", "retained.jsonl")
    files_present = all((run_dir / name).is_file() for name in required)
    file_hashes = {name: hash_file(run_dir / name) for name in required} if files_present else {}
    manifest = read_json_object(run_dir / "run.json") if files_present else {}
    status = read_json_object(run_dir / "status.json") if files_present else {}
    journal = _strict_jsonl(run_dir / "journal.jsonl") if files_present else []
    retained = _strict_jsonl(run_dir / "retained.jsonl") if files_present else []
    terminals = {
        (str(item.get("root")), str(item.get("operation_id"))): item
        for item in journal
        if item.get("kind") == "terminal"
    }
    results = [
        item
        for item in report.get("results") or []
        if isinstance(item, Mapping) and item.get("operation_id") == operation_id
    ]
    live = [
        item
        for item in results
        if item.get("expect_status") == "retained"
        and item.get("observed_status") == "retained"
        and item.get("passed") is True
    ]
    fail_closed = [
        item
        for item in results
        if item.get("expect_status") != "retained"
        and item.get("observed_status") == item.get("expect_status")
        and item.get("passed") is True
    ]
    terminal_matches = all(
        (terminal := terminals.get((str(item.get("root")), operation_id))) is not None
        and terminal.get("status") == item.get("observed_status")
        and str(terminal.get("reason", "")).startswith(str(item.get("expect_reason_prefix", "")))
        for item in results
    )
    retained_live = {
        str(cast(Mapping[str, Any], item.get("sidecar") or {}).get("root_name")): item
        for item in retained
        if cast(Mapping[str, Any], item.get("sidecar") or {}).get("operation_id") == operation_id
    }
    live_certified = bool(live) and all(
        str(item.get("root")) in retained_live
        and not release_certificate_issues(retained_live[str(item.get("root"))])
        for item in live
    )
    generator_clean = manifest.get("implementation_dirty") is False
    from leanfaith.sft1.sprint.integrity import git_commit_is_ancestor

    generator_ancestor = git_commit_is_ancestor(
        find_repo_root(Path(__file__)), manifest.get("implementation_commit")
    )
    checks = {
        "artifact_files_present": files_present,
        "report_passed": report.get("passed") is True,
        "report_run_exact": report.get("run_id") == manifest.get("run_id"),
        "operation_reported": operation_id in (report.get("success_covered") or [])
        and operation_id in (report.get("rejection_covered") or []),
        "live_retained_fixture": live_certified,
        "typed_fail_closed_fixture": bool(fail_closed),
        "fixture_terminals_exact": terminal_matches and len(results) >= 2,
        "run_final": status.get("final") is True,
        "gold_policy_exact": manifest.get("gold_blocklist_sha256") == gold_sha256,
        "clean_generator": generator_clean,
        "generator_commit_ancestor": generator_ancestor,
    }
    return {
        "path": str(resolved),
        "sha256": hash_file(resolved),
        "run_dir": str(run_dir),
        "run_id": manifest.get("run_id"),
        "file_sha256": file_hashes,
        "operation_id": operation_id,
        "live_retained_count": len(live),
        "typed_fail_closed_count": len(fail_closed),
        "checks": checks,
        "passed": all(checks.values()),
    }


def _wave3_candidate_audit_facts(
    *,
    run_dir: Path,
    operation_id: str,
    run_receipt: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, bool]]:
    """Derive the 100-candidate facts from the immutable run files themselves."""

    resolved = run_dir.resolve()
    manifest = read_json_object(resolved / "run.json")
    status = read_json_object(resolved / "status.json")
    journal = _strict_jsonl(resolved / "journal.jsonl")
    roots = manifest.get("explicit_roots")
    roots_list = [str(root) for root in roots] if isinstance(roots, list) else []
    terminals = [item for item in journal if item.get("kind") == "terminal"]
    terminal_cells = [(str(item.get("root")), str(item.get("operation_id"))) for item in terminals]
    expected_cells = {(root, operation_id) for root in roots_list}
    retained_cells = {
        (str(item.get("root")), str(item.get("operation_id")))
        for item in terminals
        if item.get("status") == "retained"
    }
    record_cells = {
        (
            str(cast(Mapping[str, Any], record.get("sidecar") or {}).get("root_name")),
            str(cast(Mapping[str, Any], record.get("sidecar") or {}).get("operation_id")),
        )
        for record in records
    }
    taxonomy = _terminal_taxonomy(journal)
    accounting = {
        "root_count": len(roots_list),
        "roots_sha256": hash_canonical(roots_list),
        "terminal_count": len(terminals),
        "terminal_cells_sha256": hash_canonical(sorted(terminal_cells)),
        "retained_terminal_count": len(retained_cells),
        "retained_row_count": len(records),
        "status_retained_total": status.get("retained_total"),
        "failure_taxonomy": taxonomy,
    }
    checks = {
        "exact_100_root_run": len(roots_list) == 100
        and len(set(roots_list)) == 100
        and status.get("roots_considered") == 100,
        "exact_operation": manifest.get("operations") == [operation_id],
        "all_100_operation_terminals": len(terminals) == 100
        and len(terminal_cells) == len(set(terminal_cells))
        and set(terminal_cells) == expected_cells,
        "retained_count_exact": retained_cells == record_cells
        and status.get("retained_total") == len(records),
        "failure_taxonomy_exact": run_receipt.get("failure_taxonomy") == taxonomy,
        "retained_certificates_exact": all(
            not release_certificate_issues(record) for record in records
        ),
        "zero_call_replay": run_receipt.get("replay_zero_call") is True,
    }
    return accounting, checks


def write_wave3_family_gate_receipt(
    *,
    operation_id: str,
    inspection_run_dir: Path,
    candidate_run_dir: Path,
    fixture_report_path: Path,
    output_dir: Path,
    gold_blocklist_path: Path,
    rows_read_by_hand: int,
    wrong_labels_found: int,
    quarantined: bool = False,
    quarantine_reason: str | None = None,
) -> dict[str, Any]:
    """Create one family gate from exact 20-root, 100-root, replay, and fixture evidence."""

    if operation_id not in WAVE3_GATE_OPERATIONS:
        raise ViewError(f"unsupported Wave 3 gate operation {operation_id!r}")
    output = output_dir.resolve()
    if output.exists():
        raise ViewError(f"{output} already exists; family gate artifacts are immutable")
    if rows_read_by_hand < 0 or wrong_labels_found < 0:
        raise ViewError("manual inspection counts must be nonnegative")
    gold_sha256 = hash_file(gold_blocklist_path.resolve())
    inspection_receipt, inspection_records = _load_completed_run(
        inspection_run_dir.resolve(), gold_sha256=gold_sha256
    )
    family_records = [
        record
        for record in inspection_records
        if cast(Mapping[str, Any], record.get("sidecar") or {}).get("operation_id") == operation_id
    ]
    inspection_performance = cast(Mapping[str, Any], inspection_receipt.get("performance") or {})
    if inspection_performance.get("roots_considered") != 20:
        raise ViewError("Wave 3 family inspection requires an exact completed 20-root run")
    if rows_read_by_hand != len(family_records):
        raise ViewError("manual inspection must cover every retained family pair")
    if not family_records and (
        not quarantined or not isinstance(quarantine_reason, str) or not quarantine_reason.strip()
    ):
        raise ViewError("a zero-yield family must be explicitly quarantined with a reason")
    if family_records and quarantined:
        raise ViewError("a useful retained family cannot be marked as zero-yield quarantined")

    candidate_receipt, candidate_records = _load_completed_run(
        candidate_run_dir.resolve(), gold_sha256=gold_sha256
    )
    candidate_family_records = [
        record
        for record in candidate_records
        if cast(Mapping[str, Any], record.get("sidecar") or {}).get("operation_id") == operation_id
    ]
    candidate_performance = cast(Mapping[str, Any], candidate_receipt.get("performance") or {})
    candidate_accounting, candidate_checks = _wave3_candidate_audit_facts(
        run_dir=candidate_run_dir,
        operation_id=operation_id,
        run_receipt=candidate_receipt,
        records=candidate_records,
    )
    fixture_receipt = _fixture_report_receipt(
        fixture_report_path.resolve(),
        operation_id=operation_id,
        gold_sha256=gold_sha256,
    )
    failed_candidate_checks = [
        name for name, passed in candidate_checks.items() if passed is not True
    ]
    if failed_candidate_checks or fixture_receipt.get("passed") is not True:
        details = ", ".join(failed_candidate_checks) or "fixture_receipt"
        raise ViewError(f"Wave 3 candidate gate evidence failed: {details}")
    audit_checks = {
        **candidate_checks,
        "run_receipt_exact": True,
        "fixture_report_hash_exact": fixture_report_path.resolve().is_file(),
        "live_and_fail_closed_fixtures": fixture_receipt.get("passed") is True,
    }
    audit = {
        "schema_version": WAVE3_GATE_SCHEMA,
        "kind": "sft1_wave3_family_100_candidate_gate_v1",
        "operation_id": operation_id,
        "typed_candidates": candidate_performance.get("roots_considered"),
        "run_dir": str(candidate_run_dir.resolve()),
        "run_id": candidate_receipt.get("run_id"),
        "run_receipt": candidate_receipt,
        "run_receipt_sha256": hash_canonical(candidate_receipt),
        "retained_family_pairs": len(candidate_family_records),
        "failure_taxonomy": candidate_receipt.get("failure_taxonomy"),
        "terminal_accounting": candidate_accounting,
        "fixture_report_path": str(fixture_report_path.resolve()),
        "fixture_report_sha256": hash_file(fixture_report_path.resolve()),
        "fixture_receipt": fixture_receipt,
        "checks": audit_checks,
        "passed": all(audit_checks.values()),
    }
    output.mkdir(parents=True)
    sample_path = output / "sample.jsonl"
    sample_bytes = b"".join(canonical_json_bytes(record) + b"\n" for record in family_records)
    write_atomic(sample_path, sample_bytes)
    audit_path = output / "candidate_audit.json"
    write_atomic(audit_path, canonical_json_bytes(audit) + b"\n")
    verdict = {
        "schema_version": WAVE3_GATE_SCHEMA,
        "kind": "sft1_wave3_family_20_root_manual_gate_v1",
        "operation_id": operation_id,
        "run_id": inspection_receipt.get("run_id"),
        "run_dir": str(inspection_run_dir.resolve()),
        "run_receipt_sha256": hash_canonical(inspection_receipt),
        "rows_read_by_hand": rows_read_by_hand,
        "wrong_labels_found": wrong_labels_found,
        "sample_path": str(sample_path),
        "sample_sha256": sha256_hex(sample_bytes),
        "candidate_audit_path": str(audit_path),
        "candidate_audit_sha256": hash_file(audit_path),
        "quarantined": quarantined,
        "quarantine_reason": quarantine_reason,
    }
    verdict_path = output / "verdict.json"
    write_atomic(verdict_path, canonical_json_bytes(verdict) + b"\n")
    validated = _inspection_receipts(
        [verdict_path],
        released_pair_ids=frozenset(_record_pair_id(record) for record in family_records),
        gold_sha256=gold_sha256,
    )
    family_validation = cast(Sequence[Mapping[str, Any]], validated["receipts"])[0]
    if family_validation.get("passed") is not True:
        raise ViewError("generated Wave 3 family gate failed exact artifact validation")
    report = {
        "operation_id": operation_id,
        "output_dir": str(output),
        "verdict_path": str(verdict_path),
        "verdict_sha256": hash_file(verdict_path),
        "sample_rows": len(family_records),
        "candidate_roots": candidate_performance.get("roots_considered"),
        "candidate_retained_pairs": len(candidate_family_records),
        "fixture_receipt": fixture_receipt,
        "passed": True,
    }
    write_atomic(output / "gate_build_report.json", canonical_json_bytes(report) + b"\n")
    return report


def _inspection_receipts(
    paths: Sequence[Path], *, released_pair_ids: frozenset[str], gold_sha256: str
) -> dict[str, Any]:
    """Validate the exact five per-family 20-root/manual/100-candidate gates."""

    receipts: list[dict[str, Any]] = []
    seen_operations: set[str] = set()
    useful_operations: set[str] = set()
    for path in paths:
        resolved = path.resolve()
        document = read_json_object(resolved)
        operation_id = document.get("operation_id")
        run_dir_value = document.get("run_dir")
        if operation_id not in WAVE3_GATE_OPERATIONS or not isinstance(run_dir_value, str):
            raise ViewError(f"Wave 3 inspection receipt has unknown family/run: {resolved}")
        duplicate_operation = operation_id in seen_operations
        seen_operations.add(operation_id)
        run_receipt, run_records = _load_completed_run(Path(run_dir_value), gold_sha256=gold_sha256)
        family_records = [
            record
            for record in run_records
            if cast(Mapping[str, Any], record.get("sidecar") or {}).get("operation_id")
            == operation_id
        ]
        family_by_pair = {_record_pair_id(record): record for record in family_records}
        run_receipt_hash = hash_canonical(run_receipt)
        sample_path_value = document.get("sample_path")
        sample_path = Path(str(sample_path_value)) if isinstance(sample_path_value, str) else None
        expected_sample_hash = document.get("sample_sha256")
        sample_verified = (
            sample_path is not None
            and sample_path.is_file()
            and isinstance(expected_sample_hash, str)
            and hash_file(sample_path) == expected_sample_hash
        )
        sampled_pair_ids: list[str] = []
        sampled_records: dict[str, dict[str, Any]] = {}
        if sample_verified and sample_path is not None:
            for record in _strict_jsonl(sample_path):
                row = record.get("row")
                sidecar = record.get("sidecar")
                pair_id = (
                    row.get("pair_id")
                    if isinstance(row, Mapping)
                    else sidecar.get("pair_id")
                    if isinstance(sidecar, Mapping)
                    else record.get("pair_id")
                )
                if isinstance(pair_id, str):
                    sampled_pair_ids.append(pair_id)
                    sampled_records[pair_id] = record
        sample_selection_bound = set(sampled_pair_ids).issubset(released_pair_ids)
        sample_exact_run = (
            len(sampled_pair_ids) == len(set(sampled_pair_ids))
            and set(sampled_pair_ids) == set(family_by_pair)
            and all(
                hash_canonical(sampled_records.get(pair_id)) == hash_canonical(record)
                for pair_id, record in family_by_pair.items()
            )
        )
        audit_path_value = document.get("candidate_audit_path")
        audit_path = Path(str(audit_path_value)) if isinstance(audit_path_value, str) else None
        audit_hash = document.get("candidate_audit_sha256")
        audit_verified = (
            audit_path is not None
            and audit_path.is_file()
            and isinstance(audit_hash, str)
            and hash_file(audit_path) == audit_hash
        )
        audit = read_json_object(audit_path) if audit_verified and audit_path is not None else {}
        candidate_run_dir = audit.get("run_dir")
        candidate_receipt: dict[str, Any] = {}
        candidate_records: list[dict[str, Any]] = []
        if isinstance(candidate_run_dir, str):
            candidate_receipt, candidate_records = _load_completed_run(
                Path(candidate_run_dir), gold_sha256=gold_sha256
            )
        candidate_family_records = [
            record
            for record in candidate_records
            if cast(Mapping[str, Any], record.get("sidecar") or {}).get("operation_id")
            == operation_id
        ]
        candidate_performance = cast(Mapping[str, Any], candidate_receipt.get("performance") or {})
        candidate_accounting: dict[str, Any] = {}
        candidate_checks: dict[str, bool] = {
            "exact_100_root_run": False,
            "exact_operation": False,
            "all_100_operation_terminals": False,
            "retained_count_exact": False,
            "failure_taxonomy_exact": False,
            "retained_certificates_exact": False,
            "zero_call_replay": False,
        }
        if isinstance(candidate_run_dir, str) and candidate_receipt:
            candidate_accounting, candidate_checks = _wave3_candidate_audit_facts(
                run_dir=Path(candidate_run_dir),
                operation_id=str(operation_id),
                run_receipt=candidate_receipt,
                records=candidate_records,
            )
        fixture_path_value = audit.get("fixture_report_path")
        fixture_path = (
            Path(str(fixture_path_value)) if isinstance(fixture_path_value, str) else None
        )
        fixture_receipt = (
            _fixture_report_receipt(
                fixture_path, operation_id=str(operation_id), gold_sha256=gold_sha256
            )
            if fixture_path is not None and fixture_path.is_file()
            else {"passed": False}
        )
        derived_audit_checks = {
            **candidate_checks,
            "run_receipt_exact": audit.get("run_receipt") == candidate_receipt
            and audit.get("run_receipt_sha256") == hash_canonical(candidate_receipt),
            "fixture_report_hash_exact": fixture_path is not None
            and fixture_path.is_file()
            and audit.get("fixture_report_sha256") == hash_file(fixture_path),
            "live_and_fail_closed_fixtures": fixture_receipt.get("passed") is True,
        }
        audit_passed = (
            audit.get("schema_version") == WAVE3_GATE_SCHEMA
            and audit.get("kind") == "sft1_wave3_family_100_candidate_gate_v1"
            and audit.get("operation_id") == operation_id
            and audit.get("run_id") == candidate_receipt.get("run_id")
            and audit.get("typed_candidates") == candidate_performance.get("roots_considered")
            and audit.get("typed_candidates") == 100
            and audit.get("retained_family_pairs") == len(candidate_family_records)
            and audit.get("failure_taxonomy") == candidate_receipt.get("failure_taxonomy")
            and audit.get("terminal_accounting") == candidate_accounting
            and audit.get("fixture_receipt") == fixture_receipt
            and audit.get("checks") == derived_audit_checks
            and all(derived_audit_checks.values())
            and audit.get("passed") is True
            and audit_verified
        )
        quarantined = document.get("quarantined") is True
        useful = bool(family_records) and not quarantined
        if useful:
            useful_operations.add(str(operation_id))
        zero_yield_handled = useful or (
            not family_records
            and quarantined
            and isinstance(document.get("quarantine_reason"), str)
            and bool(str(document["quarantine_reason"]).strip())
        )
        passed = (
            not duplicate_operation
            and document.get("run_id") == run_receipt.get("run_id")
            and document.get("run_receipt_sha256") == run_receipt_hash
            and cast(Mapping[str, Any], run_receipt["performance"]).get("roots_considered") == 20
            and document.get("wrong_labels_found") == 0
            and isinstance(document.get("rows_read_by_hand"), int)
            and int(document["rows_read_by_hand"]) == len(sampled_pair_ids)
            and sample_verified
            and sample_exact_run
            and sample_selection_bound
            and zero_yield_handled
            and audit_passed
        )
        receipts.append(
            {
                "path": str(resolved),
                "sha256": hash_file(resolved),
                "operation_id": operation_id,
                "run_id": document.get("run_id"),
                "run_dir": run_dir_value,
                "run_receipt_sha256": run_receipt_hash,
                "run_receipt": run_receipt,
                "roots_considered": cast(Mapping[str, Any], run_receipt["performance"]).get(
                    "roots_considered"
                ),
                "retained_family_pairs": len(family_records),
                "useful": useful,
                "quarantined": quarantined,
                "quarantine_reason": document.get("quarantine_reason"),
                "rows_read_by_hand": document.get("rows_read_by_hand"),
                "wrong_labels_found": document.get("wrong_labels_found"),
                "sample_path": sample_path_value,
                "sample_sha256": expected_sample_hash,
                "sample_hash_verified": sample_verified,
                "sample_pair_ids": len(sampled_pair_ids),
                "sample_pair_ids_list": sorted(sampled_pair_ids),
                "sample_pair_ids_sha256": hash_canonical(sorted(sampled_pair_ids)),
                "sample_exact_run": sample_exact_run,
                "sample_selection_bound": sample_selection_bound,
                "candidate_audit_path": audit_path_value,
                "candidate_audit_sha256": audit_hash,
                "candidate_audit_verified": audit_verified,
                "candidate_audit": audit,
                "candidate_run_receipt": candidate_receipt,
                "fixture_receipt": fixture_receipt,
                "passed": passed,
            }
        )
    exact_family_set = seen_operations == set(WAVE3_GATE_OPERATIONS) and len(receipts) == len(
        WAVE3_GATE_OPERATIONS
    )
    return {
        "provided": bool(receipts),
        "receipts": receipts,
        "required_operations": list(WAVE3_GATE_OPERATIONS),
        "exact_family_set": exact_family_set,
        "useful_operations": sorted(useful_operations),
        "useful_family_count": len(useful_operations),
        "passed": exact_family_set
        and len(useful_operations) >= 3
        and all(bool(receipt["passed"]) for receipt in receipts),
    }


def _mixed_200_gate_receipt(path: Path | None, *, gold_sha256: str) -> dict[str, Any]:
    """Validate the independent mixed-source 200-root Wave 3 gate and its run receipts."""

    if path is None:
        return {"provided": False, "passed": False}
    resolved = path.resolve()
    document = read_json_object(resolved)
    source_documents = document.get("source_runs")
    source_runs = source_documents if isinstance(source_documents, list) else []
    receipts: list[dict[str, Any]] = []
    projects: set[str] = set()
    useful: set[str] = set()
    roots = 0
    sources_passed = bool(source_runs)
    for source in source_runs:
        if not isinstance(source, Mapping) or not isinstance(source.get("run_dir"), str):
            sources_passed = False
            continue
        try:
            receipt, records = _load_completed_run(
                Path(str(source["run_dir"])), gold_sha256=gold_sha256
            )
        except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError):
            sources_passed = False
            continue
        receipt_hash = hash_canonical(receipt)
        receipt_passed = (
            source.get("run_id") == receipt.get("run_id")
            and source.get("run_receipt_sha256") == receipt_hash
            and receipt.get("replay_zero_call") is True
        )
        sources_passed = sources_passed and receipt_passed
        performance = cast(Mapping[str, Any], receipt.get("performance") or {})
        roots += int(performance.get("roots_considered") or 0)
        projects.add(str(receipt.get("project_id")))
        useful.update(
            str(cast(Mapping[str, Any], record.get("sidecar") or {}).get("operation_id"))
            for record in records
            if cast(Mapping[str, Any], record.get("sidecar") or {}).get("operation_id")
            in WAVE3_GATE_OPERATIONS
        )
        receipts.append(
            {
                "run_dir": source["run_dir"],
                "run_id": receipt.get("run_id"),
                "run_receipt_sha256": receipt_hash,
                "run_receipt": receipt,
                "project_id": receipt.get("project_id"),
                "roots_considered": performance.get("roots_considered"),
                "resume_observed": receipt.get("resume_observed"),
                "replay_zero_call": receipt.get("replay_zero_call"),
                "passed": receipt_passed,
            }
        )
    checks = document.get("checks")
    required_checks = {
        "zero_wrong_labels",
        "exact_negative_separators",
        "exact_positive_equivalences",
        "zero_self_pairs",
        "zero_partial_groups",
        "zero_conflicts",
        "zero_duplicate_stable_ids",
        "forced_resume",
        "zero_call_replay",
    }
    checks_passed = (
        isinstance(checks, Mapping)
        and required_checks.issubset(checks)
        and all(value is True for value in checks.values())
    )
    reported_useful = document.get("useful_negative_families")
    passed = (
        document.get("schema_version") == WAVE3_GATE_SCHEMA
        and document.get("kind") == "sft1_wave3_mixed_200_gate_v1"
        and document.get("roots_considered") == 200
        and roots == 200
        and len(projects) >= 2
        and isinstance(reported_useful, list)
        and {str(value) for value in reported_useful} == useful
        and len(useful) >= 3
        and document.get("wrong_labels_found") == 0
        and checks_passed
        and sources_passed
        and any(receipt.get("resume_observed") is True for receipt in receipts)
        and all(receipt.get("replay_zero_call") is True for receipt in receipts)
        and document.get("passed") is True
    )
    return {
        "provided": True,
        "path": str(resolved),
        "sha256": hash_file(resolved),
        "roots_considered": roots,
        "projects": sorted(projects),
        "useful_negative_families": sorted(useful),
        "source_runs": receipts,
        "checks": checks,
        "passed": passed,
    }


def _finite_json(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Mapping):
        return {str(key): _finite_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_finite_json(item) for item in value]
    return value


def _screen_results(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    from leanfaith.sft1.sprint import shortcut

    if not records:
        return {"rows": 0, "screens": [], "passed": False, "reason": "empty_release"}
    try:
        return cast(dict[str, Any], _finite_json(shortcut.run_screens_v3(records)))
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        return {
            "rows": len(records),
            "screens": [],
            "passed": False,
            "reason": f"screen_input_invalid:{type(exc).__name__}:{str(exc)[:200]}",
        }


def _performance_summary(receipts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    performance = [cast(Mapping[str, Any], receipt["performance"]) for receipt in receipts]

    def total(field: str) -> float:
        return sum(float(item.get(field) or 0) for item in performance)

    return {
        "lean_calls": int(total("lean_requests")),
        "lean_elapsed_ms": int(total("lean_elapsed_ms")),
        "summed_runner_wall_seconds": round(total("wall_seconds"), 3),
        "roots_considered": int(total("roots_considered")),
        "roots_from_lean": int(total("roots_lean")),
        "cache_hits": int(total("roots_cache")),
        "peak_process_tree_rss_bytes": max(
            (int(item.get("peak_process_tree_rss_bytes") or 0) for item in performance),
            default=0,
        ),
    }


def _read_stable_cache_record(path: Path) -> tuple[dict[str, Any], str]:
    try:
        before = path.read_bytes()
        value = json.loads(before.decode("utf-8"))
        after = path.read_bytes()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ViewError(f"cannot load source cache record {path}: {exc}") from exc
    if before != after:
        raise ViewError(f"source cache record changed while read: {path}")
    if not isinstance(value, dict):
        raise ViewError(f"source cache record is not a JSON object: {path}")
    return value, sha256_hex(before)


def _wave3_cache_snapshot(
    records: Sequence[dict[str, Any]], receipts: Sequence[Mapping[str, Any]]
) -> tuple[bytes, dict[str, Any]]:
    """Pack exact root/operation records and attach content-bound references."""

    from leanfaith.sft1.sprint.integrity import (
        WAVE3_CACHE_SNAPSHOT_FILE,
        WAVE3_CACHE_SNAPSHOT_SCHEMA,
        wave3_cache_record_issues,
        wave3_root_cache_key,
    )

    receipt_by_source = {str(receipt["source_key"]): receipt for receipt in receipts}
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    source_proofs: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for item in records:
        sidecar = cast(Mapping[str, Any], item["sidecar"])
        source = cast(Mapping[str, Any], item["_release_source"])
        source_key = str(source["source_key"])
        receipt = receipt_by_source.get(source_key)
        if receipt is None:
            raise ViewError(f"release row names unknown source receipt {source_key}")
        cache_root = Path(str(receipt["run_dir"])).resolve().parent.parent / "cache"
        try:
            root_key = wave3_root_cache_key(sidecar)
        except (KeyError, TypeError, ValueError) as exc:
            raise ViewError(
                f"invalid source cache identity for {_record_pair_id(item)}: {exc}"
            ) from exc
        operation_key = str(sidecar.get("cache_key", ""))
        if len(operation_key) != 64:
            raise ViewError(f"malformed operation cache key for {_record_pair_id(item)}")
        loaded: dict[str, tuple[dict[str, Any], str]] = {}
        for kind, key, cache_kind in (
            ("root", root_key, "roots"),
            ("operation", operation_key, "ops"),
        ):
            path = cache_root / cache_kind / key[:2] / f"{key}.json"
            loaded[kind] = _read_stable_cache_record(path)
        root_record, root_file_sha256 = loaded["root"]
        operation_record, operation_file_sha256 = loaded["operation"]
        binding_issues = wave3_cache_record_issues(
            sidecar,
            root_key=root_key,
            root_record=root_record,
            operation_key=operation_key,
            operation_record=operation_record,
        )
        if binding_issues:
            raise ViewError(
                f"source cache does not certify {_record_pair_id(item)}: "
                + "; ".join(binding_issues)
            )
        references: dict[str, Any] = {}
        for kind, key, record, file_sha256 in (
            ("root", root_key, root_record, root_file_sha256),
            ("operation", operation_key, operation_record, operation_file_sha256),
        ):
            identity = (kind, key)
            content_sha256 = hash_canonical(record)
            existing = entries.get(identity)
            if existing is not None and (
                existing["content_sha256"] != content_sha256 or existing["record"] != record
            ):
                raise ViewError(f"conflicting source cache records for {kind} key {key}")
            entries.setdefault(
                identity,
                {
                    "schema_version": WAVE3_CACHE_SNAPSHOT_SCHEMA,
                    "kind": kind,
                    "key": key,
                    "content_sha256": content_sha256,
                    "record": record,
                },
            )
            source_proofs.setdefault(identity, set()).add((source_key, file_sha256))
            references[kind] = {
                "key": key,
                "content_sha256": content_sha256,
                "source_file_sha256": file_sha256,
            }
        item["_release_cache"] = references

    documents: list[dict[str, Any]] = []
    for identity in sorted(entries):
        document = dict(entries[identity])
        document["sources"] = [
            {"source_key": source_key, "file_sha256": file_sha256}
            for source_key, file_sha256 in sorted(source_proofs[identity])
        ]
        documents.append(document)
    snapshot_bytes = b"".join(canonical_json_bytes(document) + b"\n" for document in documents)
    identities = [
        {
            "kind": document["kind"],
            "key": document["key"],
            "content_sha256": document["content_sha256"],
            "sources": document["sources"],
        }
        for document in documents
    ]
    manifest = {
        "schema_version": WAVE3_CACHE_SNAPSHOT_SCHEMA,
        "file": WAVE3_CACHE_SNAPSHOT_FILE,
        "file_sha256": sha256_hex(snapshot_bytes),
        "record_count": len(documents),
        "root_records": sum(document["kind"] == "root" for document in documents),
        "operation_records": sum(document["kind"] == "operation" for document in documents),
        "content_set_sha256": hash_canonical(identities),
    }
    return snapshot_bytes, manifest


def build_wave3_release(
    *,
    repo_root: Path,
    run_dirs: Sequence[Path],
    output_dir: Path,
    gold_blocklist_path: Path,
    label: str = "wave3/natural_core_v1",
    shard_size: int = 1_000,
    maximum_rows: int | None = None,
    n25_cap_fraction: float = 0.25,
    inspection_verdict_paths: Sequence[Path] = (),
    mixed_200_gate_report: Path | None = None,
) -> dict[str, Any]:
    """Build a deterministic, no-regeneration Wave 3 release from completed runs."""

    if shard_size < 1:
        raise ViewError("shard_size must be positive")
    if not run_dirs:
        raise ViewError("at least one explicit completed run directory is required")
    resolved_runs = [path.resolve() for path in run_dirs]
    if len(resolved_runs) != len(set(resolved_runs)):
        raise ViewError("completed run directories must be distinct")
    out = output_dir.resolve()
    if out.exists():
        raise ViewError(f"{out} already exists; Wave 3 releases are additive and immutable")
    release_builder = _git_identity(repo_root.resolve())
    if release_builder["dirty"] is not False:
        raise ViewError("publishable Wave 3 release requires a clean release-builder worktree")

    gold = GoldBlocklist.load(gold_blocklist_path.resolve())
    receipts: list[dict[str, Any]] = []
    source_records: list[dict[str, Any]] = []
    for run_dir in sorted(resolved_runs, key=str):
        receipt, records = _load_completed_run(run_dir, gold_sha256=gold.sha256)
        receipts.append(receipt)
        for source_record in records:
            source_hash = hash_canonical(source_record)
            record = json.loads(json.dumps(source_record))
            reason = _source_record_issue(record, receipt, gold)
            record["_screen_rejection"] = reason
            record["_source_record_sha256"] = source_hash
            record["_release_source"] = {
                "source_key": receipt["source_key"],
                "run_id": receipt["run_id"],
                "project_id": receipt["project_id"],
                "project_revision": receipt["project_revision"],
            }
            record["_pair_delta"] = wave3_pair_delta(record)
            source_records.append(record)

    projects = {str(receipt["project_id"]) for receipt in receipts}
    if projects != WAVE3_PROJECTS:
        raise ViewError(
            "Wave 3 release requires explicit Mathlib, Physlib, and CSLib inputs; "
            f"observed {sorted(projects)}"
        )

    collapsed, exact_repeats = _collapse_exact_pair_ids(source_records)
    screen_rejections: dict[str, int] = {}
    for record in collapsed:
        reason = record["_screen_rejection"]
        if isinstance(reason, str):
            screen_rejections[reason] = screen_rejections.get(reason, 0) + 1
    labels_by_pair: dict[str, set[bool]] = {}
    for record in collapsed:
        labels_by_pair.setdefault(str(record["unordered_pair_key"]), set()).add(
            bool(record["label"])
        )
    conflicts = sorted(key for key, labels in labels_by_pair.items() if len(labels) > 1)
    if conflicts:
        raise ViewError(
            f"cross-source conflicting labels for {len(conflicts)} unordered pairs; "
            f"first={conflicts[0]}"
        )
    screened = [record for record in collapsed if record["_screen_rejection"] is None]
    dedup = deduplicate(screened)
    if dedup.conflict_count:
        raise ViewError("conflicting unordered pairs survived the explicit conflict check")
    deduplicated = [cast(dict[str, Any], record) for record in dedup.kept]
    selected, selection = _balanced_wave3_selection(
        deduplicated,
        maximum_rows=maximum_rows,
        n25_cap_fraction=n25_cap_fraction,
    )
    release_id = "wave3_release:" + hash_canonical(
        {
            "schema_version": WAVE3_RELEASE_SCHEMA,
            "label": label,
            "source_hashes": [
                receipt["input_sha256"]
                for receipt in sorted(receipts, key=lambda item: str(item["source_key"]))
            ],
            "gold_blocklist_sha256": gold.sha256,
            "selection": {
                "salt": WAVE3_RELEASE_SALT,
                "maximum_rows": maximum_rows,
                "n25_cap_fraction": n25_cap_fraction,
            },
        }
    )
    cache_snapshot_bytes, cache_snapshot_manifest = _wave3_cache_snapshot(selected, receipts)

    release_records: list[dict[str, Any]] = []
    for record in selected:
        row = cast(Mapping[str, Any], record["row"])
        sidecar = json.loads(json.dumps(record["sidecar"]))
        if "release" in sidecar:
            raise ViewError(
                f"source sidecar already has release metadata: {_record_pair_id(record)}"
            )
        mechanism = mechanism_of(str(record["operation_id"]))
        sidecar["core_family"] = str(sidecar.get("core_family") or mechanism)
        sidecar["row_schema"] = "sft_core_v1"
        sidecar["release"] = {
            "schema_version": WAVE3_RELEASE_SCHEMA,
            "release_id": release_id,
            "source": record["_release_source"],
            "source_record_sha256": record["_source_record_sha256"],
            "pair_delta": record["_pair_delta"],
            "selection_policy": selection["policy"],
            "source_cache": record["_release_cache"],
        }
        model_row = {
            "reference": row["reference"],
            "candidate": row["candidate"],
            "label": row["label"],
        }
        release_records.append(
            {
                "row": model_row,
                "sidecar": sidecar,
                "row_hash": record["row_hash"],
                "unordered_pair_key": record["unordered_pair_key"],
                "label": record["label"],
                "operation_id": record["operation_id"],
                "mechanism": mechanism,
                "root_name": record["root_name"],
            }
        )

    out.mkdir(parents=True)
    source_cache_path = out / str(cache_snapshot_manifest["file"])
    write_atomic(source_cache_path, cache_snapshot_bytes)
    shard_manifests: list[dict[str, Any]] = []
    for number, shard in enumerate(_release_shards(release_records, shard_size), start=1):
        shard_dir = out / f"shard-{number:04d}"
        shard_dir.mkdir()
        rows_bytes = b"".join(canonical_json_bytes(record["row"]) + b"\n" for record in shard)
        sidecars_bytes = b"".join(
            canonical_json_bytes(record["sidecar"]) + b"\n" for record in shard
        )
        write_atomic(shard_dir / "rows.jsonl", rows_bytes)
        write_atomic(shard_dir / "sidecars.jsonl", sidecars_bytes)
        shard_manifest = {
            "schema_version": WAVE3_RELEASE_SCHEMA,
            "row_schema": "sft_core_v1",
            "release_id": release_id,
            "view": label,
            "shard": number,
            "row_count": len(shard),
            "complete": True,
            "finalized": True,
            "labels": {
                "positive": sum(bool(record["label"]) for record in shard),
                "negative": sum(not bool(record["label"]) for record in shard),
            },
            "roots": len({str(record["sidecar"]["root_id"]) for record in shard}),
            "projects": _count_nested(shard, "sidecar", "release", "source", "project_id"),
            "operations": _count_by(shard, "operation_id"),
            "mechanisms": _count_by(shard, "mechanism"),
            "rows_sha256": sha256_hex(rows_bytes),
            "sidecars_sha256": sha256_hex(sidecars_bytes),
        }
        shard_manifest["content_sha256"] = hash_canonical(shard_manifest)
        write_atomic(shard_dir / "manifest.json", canonical_json_bytes(shard_manifest) + b"\n")
        shard_manifests.append(shard_manifest)

    from leanfaith.sft1.sprint import shortcut

    screen_sample, sample_receipt = shortcut.screen_sample(out)
    screens = _screen_results(screen_sample)
    pair_delta_diagnostics = shortcut.pairwise_shortcut_diagnostics(release_records)
    inspections = _inspection_receipts(
        inspection_verdict_paths,
        released_pair_ids=frozenset(
            str(record["sidecar"]["pair_id"]) for record in release_records
        ),
        gold_sha256=gold.sha256,
    )
    mixed_gate = _mixed_200_gate_receipt(mixed_200_gate_report, gold_sha256=gold.sha256)
    gate_checks = {
        "five_family_20_root_manual_gates": inspections["passed"] is True,
        "at_least_three_useful_negative_families": inspections["useful_family_count"] >= 3,
        "five_family_100_candidate_and_fixture_gates": all(
            receipt.get("candidate_audit", {}).get("passed") is True
            for receipt in inspections["receipts"]
        )
        and len(inspections["receipts"]) == len(WAVE3_GATE_OPERATIONS),
        "mixed_source_200_root_gate": mixed_gate["passed"] is True,
    }
    wave3_gate = {
        "schema_version": WAVE3_GATE_SCHEMA,
        "kind": "sft1_wave3_release_gate_v1",
        "release_id": release_id,
        "family_gates": inspections,
        "mixed_200_gate": mixed_gate,
        "checks": gate_checks,
        "passed": all(gate_checks.values()),
    }
    wave3_gate["content_binding_sha256"] = hash_canonical(wave3_gate)
    wave3_gate_bytes = canonical_json_bytes(wave3_gate) + b"\n"
    write_atomic(out / "wave3_gate_report.json", wave3_gate_bytes)
    labels = {
        "positive": sum(bool(record["label"]) for record in release_records),
        "negative": sum(not bool(record["label"]) for record in release_records),
    }
    operations = _count_by(release_records, "operation_id")
    n25_rows = operations.get(N25_OPERATION, 0)
    n25_share = n25_rows / len(release_records) if release_records else 0.0
    released_projects = _count_nested(release_records, "sidecar", "release", "source", "project_id")
    selected_cells = cast(Mapping[str, Mapping[str, int]], selection["pair_delta_cells_selected"])
    pair_delta_balanced = all(
        values["positive"] == values["negative"] for values in selected_cells.values()
    )
    screen_by_name = {
        str(item.get("name")): item
        for item in cast(Sequence[Mapping[str, Any]], screens.get("screens", []))
    }
    conservation: dict[str, Any] = {
        "input_records": len(source_records),
        "exact_repeats_removed": exact_repeats,
        "screen_rejections": dict(sorted(screen_rejections.items())),
        "same_label_duplicates_removed": dedup.duplicate_count,
        "balance_or_capacity_rows_dropped": len(deduplicated) - len(release_records),
        "released_rows": len(release_records),
    }
    conservation["holds"] = conservation["input_records"] == (
        conservation["exact_repeats_removed"]
        + sum(cast(dict[str, int], conservation["screen_rejections"]).values())
        + conservation["same_label_duplicates_removed"]
        + conservation["balance_or_capacity_rows_dropped"]
        + conservation["released_rows"]
    )
    checks = {
        "nonempty": bool(release_records),
        "exact_three_projects": projects == WAVE3_PROJECTS,
        "released_rows_cover_all_three_projects": set(released_projects) == WAVE3_PROJECTS,
        "source_runs_terminal_authorized": all(
            all(cast(Mapping[str, bool], receipt["checks"]).values()) for receipt in receipts
        ),
        "rows_exactly_reference_candidate_label": all(
            set(record["row"]) == MODEL_ROW_FIELDS for record in release_records
        ),
        "zero_n19": not any(_is_n19(operation, "") for operation in operations),
        "n25_at_most_configured_cap": n25_share <= n25_cap_fraction,
        "labels_balanced": labels["positive"] == labels["negative"],
        "pair_delta_cells_balanced": pair_delta_balanced,
        "zero_conflicts": not conflicts and dedup.conflict_count == 0,
        "zero_duplicate_stable_ids": len(
            {str(record["sidecar"]["pair_id"]) for record in release_records}
        )
        == len(release_records),
        "all_shards_independently_complete": all(
            shard["complete"] is True and shard["finalized"] is True for shard in shard_manifests
        ),
        "conservation": bool(conservation["holds"]),
        "candidate_only_screen": screen_by_name.get("candidate_only", {}).get("passed") is True,
        "reference_only_screen": screen_by_name.get("reference_only", {}).get("passed") is True,
        "family_held_out_screen": screen_by_name.get("family_held_out", {}).get("passed") is True,
        "manual_inspection": bool(inspections["passed"]),
        "wave3_release_gate": wave3_gate["passed"] is True,
    }
    receipt_documents = sorted(receipts, key=lambda item: str(item["source_key"]))
    source_retained_files = [
        {
            "source_key": receipt["source_key"],
            "run_id": receipt["run_id"],
            "project_id": receipt["project_id"],
            "path": str(Path(str(receipt["run_dir"])) / "retained.jsonl"),
            "sha256": cast(Mapping[str, Any], receipt["input_sha256"])["retained"],
        }
        for receipt in receipt_documents
    ]
    from leanfaith.sft1.sprint.integrity import (
        derive_wave3_snapshot_provenance,
        validate_view,
    )

    provenance, snapshot_issues = derive_wave3_snapshot_provenance(
        release_records,
        repo_root=repo_root,
        release_dir=out,
        snapshot=cache_snapshot_manifest,
    )
    if snapshot_issues:
        raise ViewError("source cache snapshot invalid: " + "; ".join(snapshot_issues))
    if not provenance["consistent"]:
        raise ViewError("provenance inconsistent: " + "; ".join(provenance["issues"]))
    manifest = {
        "schema_version": WAVE3_RELEASE_SCHEMA,
        "row_schema": "sft_core_v1",
        "row_fields": sorted(MODEL_ROW_FIELDS),
        "release_id": release_id,
        "view": label,
        "finalized": True,
        "artifact_status": (
            "proof_certified_release" if all(checks.values()) else "release_candidate_gate_failed"
        ),
        "retained_rows": len(release_records),
        "roots": len({str(record["sidecar"]["root_id"]) for record in release_records}),
        "labels": labels,
        "projects": released_projects,
        "operations": operations,
        "mechanisms": _count_by(release_records, "mechanism"),
        "pair_delta_cells": _count_nested(
            release_records, "sidecar", "release", "pair_delta", "cell"
        ),
        "shard_size": shard_size,
        "shards": shard_manifests,
        "source_runs": receipt_documents,
        "source_receipts_sha256": hash_canonical(receipt_documents),
        "source_retained_paths": [item["path"] for item in source_retained_files],
        "source_retained_files": source_retained_files,
        "source_cache_snapshot": cache_snapshot_manifest,
        "cache_snapshots": [
            {
                **cache_snapshot_manifest,
                "sha256": cache_snapshot_manifest["file_sha256"],
            }
        ],
        "wave3_gate": {
            "file": "wave3_gate_report.json",
            "sha256": sha256_hex(wave3_gate_bytes),
            "content_binding_sha256": wave3_gate["content_binding_sha256"],
            "passed": wave3_gate["passed"],
        },
        "multiple_project_pins_allowed": True,
        "provenance": provenance,
        "input_records": conservation["input_records"],
        "screen_rejections": conservation["screen_rejections"],
        "duplicates_removed": conservation["same_label_duplicates_removed"],
        "repeated_input_records_dropped": conservation["exact_repeats_removed"],
        "conflicting_rows_rejected": 0,
        "view_dropped": conservation["balance_or_capacity_rows_dropped"],
        "gold_blocklist_sha256": gold.sha256,
        "release_builder": release_builder,
        "selection": selection,
        "proof_check_time": "original_generation",
        "replay_semantics": "journal_and_cache_replay_of_stored_terminals_no_fresh_kernel_replay",
    }
    write_atomic(out / "manifest.json", canonical_json_bytes(manifest) + b"\n")
    manifest_sha256 = hash_file(out / "manifest.json")
    integrity = validate_view(
        repo_root=repo_root,
        staging_root=out.parent,
        run_id=release_id,
        compacted_dir=out,
        source_runs=receipt_documents,
    )
    if integrity["passed"] is not True:
        raise ViewError("Wave 3 release integrity failed: " + "; ".join(integrity["issues"][:20]))
    checks["integrity_report"] = True
    integrity_report_sha256 = hash_file(out / "integrity_report.json")
    report = {
        "schema_version": WAVE3_RELEASE_SCHEMA,
        "release_id": release_id,
        "view": label,
        "artifact_status": manifest["artifact_status"],
        "manifest_sha256": manifest_sha256,
        "integrity_report_sha256": integrity_report_sha256,
        "rows": len(release_records),
        "unique_ancestry_roots": manifest["roots"],
        "labels": labels,
        "yields": {
            "by_source": manifest["projects"],
            "by_operation": operations,
            "by_negative_family": {
                operation: count
                for operation, count in operations.items()
                if operation in NEGATIVE_OPERATIONS
            },
            "by_preserving_family": {
                operation: count
                for operation, count in operations.items()
                if operation in POSITIVE_OPERATIONS
            },
        },
        "n19_rows": 0,
        "n25_rows": n25_rows,
        "n25_share": round(n25_share, 8),
        "selection": selection,
        "conservation": conservation,
        "failure_taxonomy": {
            str(receipt["source_key"]): receipt["failure_taxonomy"] for receipt in receipts
        },
        "performance": _performance_summary(receipts),
        "resume_replay": {
            "all_zero_call": all(bool(receipt["replay_zero_call"]) for receipt in receipts),
            "forced_resume_observed": any(bool(receipt["resume_observed"]) for receipt in receipts),
            "source_runs": [
                {
                    "source_key": receipt["source_key"],
                    "run_id": receipt["run_id"],
                    "resume_observed": receipt["resume_observed"],
                    "replay_zero_call": receipt["replay_zero_call"],
                }
                for receipt in receipt_documents
            ],
        },
        "manual_inspection": inspections,
        "wave3_gate": wave3_gate,
        "screen_sample": sample_receipt,
        "shortcut_screens": screens,
        "pair_delta_diagnostics": pair_delta_diagnostics,
        "integrity": {
            "rows_checked": integrity["rows_checked"],
            "source_retained_files_checked": integrity["source_retained_files_checked"],
            "source_cache_snapshot_records_checked": integrity[
                "source_cache_snapshot_records_checked"
            ],
            "passed": integrity["passed"],
        },
        "checks": checks,
        "passed": all(checks.values()),
    }
    report["content_binding_sha256"] = hash_canonical(
        {
            "manifest_sha256": manifest_sha256,
            "integrity_report_sha256": integrity_report_sha256,
            "checks": checks,
            "shortcut_screens": screens,
            "pair_delta_diagnostics": pair_delta_diagnostics,
            "manual_inspection": inspections,
            "wave3_gate": wave3_gate,
            "conservation": conservation,
        }
    )
    write_atomic(out / "release_report.json", canonical_json_bytes(report) + b"\n")
    return report


def _wave3_release_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Build an additive multi-project Wave 3 release")
    parser.add_argument("--repo-root", type=Path, default=find_repo_root(Path.cwd()))
    parser.add_argument(
        "--run-dir",
        action="append",
        type=Path,
        required=True,
        help="explicit completed runner directory; repeat for every source run",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--gold-blocklist", type=Path)
    parser.add_argument("--label", default="wave3/natural_core_v1")
    parser.add_argument("--shard-size", type=int, default=1_000)
    parser.add_argument("--maximum-rows", type=int)
    parser.add_argument("--n25-cap", type=float, default=0.25)
    parser.add_argument("--inspection-verdict", action="append", type=Path, default=[])
    parser.add_argument(
        "--mixed-200-gate-report",
        type=Path,
        help="passed mixed-source 200-root Wave 3 gate report",
    )
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    gold = (
        args.gold_blocklist.resolve()
        if args.gold_blocklist
        else repo_root / "data/benchmarks/golden_blocklist_v1.json"
    )
    report = build_wave3_release(
        repo_root=repo_root,
        run_dirs=args.run_dir,
        output_dir=args.output_dir,
        gold_blocklist_path=gold,
        label=args.label,
        shard_size=args.shard_size,
        maximum_rows=args.maximum_rows,
        n25_cap_fraction=args.n25_cap,
        inspection_verdict_paths=args.inspection_verdict,
        mixed_200_gate_report=args.mixed_200_gate_report,
    )
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report["passed"] else 1


def _wave3_family_gate_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Build one immutable Wave 3 family inspection/audit receipt"
    )
    parser.add_argument("--operation", choices=WAVE3_GATE_OPERATIONS, required=True)
    parser.add_argument("--inspection-run-dir", type=Path, required=True)
    parser.add_argument("--candidate-run-dir", type=Path, required=True)
    parser.add_argument("--fixture-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--gold-blocklist", type=Path, required=True)
    parser.add_argument("--rows-read-by-hand", type=int, required=True)
    parser.add_argument("--wrong-labels-found", type=int, required=True)
    parser.add_argument("--quarantined", action="store_true")
    parser.add_argument("--quarantine-reason")
    args = parser.parse_args(argv)
    report = write_wave3_family_gate_receipt(
        operation_id=args.operation,
        inspection_run_dir=args.inspection_run_dir,
        candidate_run_dir=args.candidate_run_dir,
        fixture_report_path=args.fixture_report,
        output_dir=args.output_dir,
        gold_blocklist_path=args.gold_blocklist,
        rows_read_by_hand=args.rows_read_by_hand,
        wrong_labels_found=args.wrong_labels_found,
        quarantined=args.quarantined,
        quarantine_reason=args.quarantine_reason,
    )
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report["passed"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "wave3-family-gate":
        return _wave3_family_gate_main(arguments[1:])
    if arguments and arguments[0] == "wave3-release":
        return _wave3_release_main(arguments[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=find_repo_root(Path.cwd()))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--runs", required=True, help="comma-separated run ids to join")
    parser.add_argument("--label", default="core_v2")
    parser.add_argument("--n31-cap", type=float, default=0.02)
    parser.add_argument("--order-cap", type=float, default=None)
    args = parser.parse_args(arguments)
    repo_root = args.repo_root.resolve()
    loaded = load_sprint_config(repo_root, args.config.resolve() if args.config else None)
    report = build_views(
        repo_root,
        loaded,
        run_ids=args.runs.split(","),
        label=args.label,
        n31_cap_fraction=args.n31_cap,
        order_cap_fraction=args.order_cap,
    )
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

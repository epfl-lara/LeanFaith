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
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, sha256_hex
from leanfaith.config.loading import LoadedConfig
from leanfaith.config.paths import find_repo_root
from leanfaith.sft1.sprint.engine import NEGATIVE_OPERATIONS, POSITIVE_OPERATIONS
from leanfaith.sft1.sprint.provenance import derive_provenance
from leanfaith.sft1.sprint.runner import (
    RunPaths,
    SprintConfig,
    _count_by,
    ancestry_shards,
    group_by_ancestry,
    load_sprint_config,
    read_retained,
    utc_now,
)
from leanfaith.sft1.sprint.screens import GoldBlocklist, deduplicate, residue_violation
from leanfaith.sft1.sprint.store import write_atomic

CORE_SALT = "sft1_sprint_core_v2"
TWIN_PRIORITY_ORDER = (
    "P18_SYMMETRIZE_EQUALITY_V1",
    "P14_SWAP_INDEPENDENT_DATA_BINDERS_V1",
    "P15_SWAP_IFF_SIDES_V1",
    "P23_CURRY_PROP_PAIR_V1",
    "P_DROP_REDUNDANT_GUARD_PROOF_V1",
    "P_NE_SYMMETRIZE_V1",
)
TWIN_PRIORITY_GUARD = (
    "P_DROP_REDUNDANT_GUARD_PROOF_V1",
    "P18_SYMMETRIZE_EQUALITY_V1",
    "P14_SWAP_INDEPENDENT_DATA_BINDERS_V1",
    "P15_SWAP_IFF_SIDES_V1",
    "P23_CURRY_PROP_PAIR_V1",
    "P_NE_SYMMETRIZE_V1",
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
    records: Sequence[dict[str, Any]], *, n31_cap_fraction: float = 0.02
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
    for root in sorted(ops_by_root, key=root_rank):
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
    joined_stats = {
        "input_records": len(records),
        "input_by_run": _count_by(records, "source_run"),
        "screen_rejections": rejections,
        "duplicates_removed": outcome.duplicate_count,
        "conflicting_classes_rejected": outcome.conflict_count,
        "deduplicated_records": len(outcome.kept),
        "artifact_status": "candidate_model_facing_view",
        "gold_blocklist_sha256": gold.sha256,
    }
    core, core_report = build_core(outcome.kept, n31_cap_fraction=n31_cap_fraction)
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
        "zero_conflicting_pairs": outcome.conflict_count == 0,
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=find_repo_root(Path.cwd()))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--runs", required=True, help="comma-separated run ids to join")
    parser.add_argument("--label", default="core_v2")
    parser.add_argument("--n31-cap", type=float, default=0.02)
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    loaded = load_sprint_config(repo_root, args.config.resolve() if args.config else None)
    report = build_views(
        repo_root,
        loaded,
        run_ids=args.runs.split(","),
        label=args.label,
        n31_cap_fraction=args.n31_cap,
    )
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

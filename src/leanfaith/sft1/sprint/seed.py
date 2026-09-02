"""Additive corrected seed release built from already certified rows.

``core_v2_seed`` reuses the matched relation design of ``core_v2`` but stores
model-facing rows with exactly ``{reference, candidate, label}``, keeps every
identifier in the sidecar, applies an exact deterministic 50% orientation
(one swapped row per paired root), marks the finalized shard complete, and
evaluates the order-invariant screens on the serialized shards themselves.
No Lean runs and no certified pair is regenerated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from leanfaith.config.hashing import canonical_json_bytes, sha256_hex
from leanfaith.config.loading import LoadedConfig
from leanfaith.config.paths import find_repo_root
from leanfaith.sft1.sprint.engine import NEGATIVE_OPERATIONS, POSITIVE_OPERATIONS, mechanism_of
from leanfaith.sft1.sprint.provenance import derive_provenance
from leanfaith.sft1.sprint.runner import SprintConfig, _count_by, load_sprint_config, utc_now
from leanfaith.sft1.sprint.screens import GoldBlocklist, deduplicate
from leanfaith.sft1.sprint.store import write_atomic
from leanfaith.sft1.sprint.views import build_core, load_runs, root_rank, screen_records

SEED_SALT = "sft1_sprint_core_v2_seed"
ROW_SCHEMA = "sft_core_v1"
VIEW_FIELDS = ("orientation", "core_family", "core_cell")
EXPLORATORY_NEGATIVES = frozenset({"N31_DROP_REQUIRED_GUARD_PROOF_V1"})


class SeedError(RuntimeError):
    """Fail-closed seed construction error."""


def swapped_polarity(root_id: str) -> bool:
    """Which polarity of a paired root is stored swapped: True → positive."""

    digest = hashlib.sha256(f"{SEED_SALT}|{root_id}".encode()).hexdigest()
    return int(digest, 16) % 2 == 0


def diversity_floor(total_rows: int) -> int:
    return min(100, math.ceil(0.05 * total_rows))


def _original_row(record: Mapping[str, Any]) -> dict[str, Any]:
    """Undo any stored orientation so the seed assignment starts from the source."""

    row = dict(record["row"])
    if record["sidecar"].get("orientation") == "swapped":
        row["reference"], row["candidate"] = row["candidate"], row["reference"]
    return row


def seed_records(core: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Three-field rows, metadata sidecars, exactly one swapped row per root."""

    by_root: dict[str, list[Mapping[str, Any]]] = {}
    for record in core:
        by_root.setdefault(str(record["row"]["root_id"]), []).append(record)
    out: list[dict[str, Any]] = []
    for root_id in sorted(by_root, key=root_rank):
        members = by_root[root_id]
        labels = sorted(bool(item["label"]) for item in members)
        if labels != [False, True]:
            raise SeedError(f"root {root_id} does not hold exactly one row per polarity")
        swap_positive = swapped_polarity(root_id)
        for item in sorted(members, key=lambda entry: str(entry["row_hash"])):
            original = _original_row(item)
            label = bool(original["label"])
            swapped = label == swap_positive
            reference, candidate = original["reference"], original["candidate"]
            if swapped:
                reference, candidate = candidate, reference
            sidecar = {k: v for k, v in item["sidecar"].items() if k not in VIEW_FIELDS}
            operation = str(original["operation_id"])
            sidecar.update(
                {
                    "pair_id": original["pair_id"],
                    "root_id": original["root_id"],
                    "operation_id": operation,
                    "mechanism": mechanism_of(operation),
                    "label": label,
                    "row_schema": ROW_SCHEMA,
                    "orientation": "swapped" if swapped else "original",
                    "stored_reference_is": "candidate" if swapped else "reference",
                    "core_family": item["sidecar"]["core_family"],
                    "core_cell": item["sidecar"]["core_cell"],
                    "orientation_rule": "one_swapped_row_per_paired_root",
                }
            )
            out.append(
                {
                    "row": {"reference": reference, "candidate": candidate, "label": label},
                    "sidecar": sidecar,
                    "row_hash": item["row_hash"],
                    "unordered_pair_key": item["unordered_pair_key"],
                    "label": label,
                    "operation_id": operation,
                    "root_name": item["root_name"],
                    "mechanism": mechanism_of(operation),
                }
            )
    return out


def write_seed_view(
    *,
    repo_root: Path,
    loaded: LoadedConfig[SprintConfig],
    label: str,
    records: Sequence[dict[str, Any]],
    source_runs: Sequence[str],
    extra: Mapping[str, Any],
) -> dict[str, Any]:
    config = loaded.config
    out = Path(config.output.staging_root) / "compacted" / label
    if out.exists():
        raise SeedError(f"{out} already exists; seed views are additive and immutable")
    out.mkdir(parents=True)
    size = config.output.shard_size
    provenance = derive_provenance(
        records, repo_root=repo_root, cache_root=Path(config.output.staging_root) / "cache"
    )
    if not provenance["consistent"]:
        raise SeedError("provenance inconsistent: " + "; ".join(provenance["issues"]))
    shards: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_root: str | None = None
    for record in records:
        root_id = str(record["sidecar"]["root_id"])
        if current and len(current) >= size and root_id != current_root:
            shards.append(current)
            current = []
        current.append(record)
        current_root = root_id
    if current:
        shards.append(current)
    shard_manifests = []
    for number, shard in enumerate(shards, start=1):
        shard_dir = out / f"shard-{number:04d}"
        shard_dir.mkdir()
        rows_bytes = b"".join(canonical_json_bytes(item["row"]) + b"\n" for item in shard)
        sidecar_bytes = b"".join(canonical_json_bytes(item["sidecar"]) + b"\n" for item in shard)
        write_atomic(shard_dir / "rows.jsonl", rows_bytes)
        write_atomic(shard_dir / "sidecars.jsonl", sidecar_bytes)
        manifest = {
            "schema_version": 2,
            "row_schema": ROW_SCHEMA,
            "view": label,
            "shard": number,
            "row_count": len(shard),
            "complete": True,
            "finalized": True,
            "labels": {
                "positive": sum(1 for item in shard if item["label"]),
                "negative": sum(1 for item in shard if not item["label"]),
            },
            "operations": _count_by(shard, "operation_id"),
            "mechanisms": _count_by(shard, "mechanism"),
            "roots": len({item["sidecar"]["root_id"] for item in shard}),
            "rows_sha256": sha256_hex(rows_bytes),
            "sidecars_sha256": sha256_hex(sidecar_bytes),
            "engine_source_sha256_set": sorted(
                {str(item["sidecar"]["engine"]["source_sha256"]) for item in shard}
            ),
        }
        write_atomic(shard_dir / "manifest.json", canonical_json_bytes(manifest) + b"\n")
        shard_manifests.append(manifest)
    manifest = {
        "schema_version": 2,
        "row_schema": ROW_SCHEMA,
        "row_fields": ["reference", "candidate", "label"],
        "sprint_id": config.sprint_id,
        "run_id": label,
        "view": label,
        "source_runs": list(source_runs),
        "compacted_at": utc_now(),
        "finalized": True,
        "retained_rows": len(records),
        "labels": {
            "positive": sum(1 for item in records if item["label"]),
            "negative": sum(1 for item in records if not item["label"]),
        },
        "operations": _count_by(records, "operation_id"),
        "mechanisms": _count_by(records, "mechanism"),
        "roots": len({item["sidecar"]["root_id"] for item in records}),
        "orientation": _count_by([item["sidecar"] for item in records], "orientation"),
        "orientation_rule": "one_swapped_row_per_paired_root",
        "shard_size": size,
        "shards": shard_manifests,
        "config_semantic_hash": loaded.config_hash,
        "provenance": provenance,
        "proof_check_time": "original_generation",
        "replay_semantics": "journal_and_cache_replay_of_stored_terminals_no_fresh_kernel_replay",
        **dict(extra),
    }
    write_atomic(out / "manifest.json", canonical_json_bytes(manifest) + b"\n")
    return manifest


def build_seed(
    repo_root: Path,
    loaded: LoadedConfig[SprintConfig],
    *,
    run_ids: Sequence[str] = ("tenk", "v2_ne", "v2_lt"),
    label: str = "core_v2_seed",
    n31_cap_fraction: float = 0.02,
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
    core, core_report = build_core(outcome.kept, n31_cap_fraction=n31_cap_fraction)
    seeds = seed_records(core)
    joined = {
        "input_records": len(records),
        "input_by_run": _count_by(records, "source_run"),
        "screen_rejections": rejections,
        "duplicates_removed": outcome.duplicate_count,
        "conflicting_classes_rejected": outcome.conflict_count,
        "deduplicated_records": len(outcome.kept),
        "view_dropped": len(outcome.kept) - len(seeds),
        "gold_blocklist_sha256": gold.sha256,
        "core_report": core_report,
        "artifact_status": "candidate_seed_release_pending_gate",
    }
    manifest = write_seed_view(
        repo_root=repo_root,
        loaded=loaded,
        label=label,
        records=seeds,
        source_runs=run_ids,
        extra=joined,
    )
    out = Path(config.output.staging_root) / "compacted" / label
    serialized = shortcut.load_serialized_view(out)
    screens = shortcut.run_screens_v3(serialized)
    total = len(serialized)
    floor = diversity_floor(total)
    negatives_by_mechanism: dict[str, int] = {}
    unchecked = 0
    for item in serialized:
        sidecar = item["sidecar"]
        operation = str(sidecar["operation_id"])
        if operation in NEGATIVE_OPERATIONS:
            mechanism = mechanism_of(operation)
            negatives_by_mechanism[mechanism] = negatives_by_mechanism.get(mechanism, 0) + 1
        evidence = sidecar.get("evidence") or {}
        check = (
            (evidence.get("equivalence_proof") or {}).get("check")
            if operation in POSITIVE_OPERATIONS
            else (evidence.get("refutation") or {}).get("check")
        )
        if not check or not check.get("meta_checked") or not check.get("kernel_checked"):
            unchecked += 1
    countable = {
        mechanism: count
        for mechanism, count in negatives_by_mechanism.items()
        if mechanism not in {mechanism_of(op) for op in EXPLORATORY_NEGATIVES}
    }
    qualifying = sorted(mechanism for mechanism, count in countable.items() if count >= floor)
    swapped = sum(1 for item in serialized if item["sidecar"].get("orientation") == "swapped")
    roots = {str(item["sidecar"]["root_id"]) for item in serialized}
    per_root_swaps: dict[str, int] = {}
    for item in serialized:
        root = str(item["sidecar"]["root_id"])
        per_root_swaps[root] = per_root_swaps.get(root, 0) + (
            1 if item["sidecar"].get("orientation") == "swapped" else 0
        )
    screen_by_name = {str(s["name"]): s for s in cast(list[dict[str, Any]], screens["screens"])}
    checks = {
        "core_nonempty": total > 0,
        "rows_are_exactly_reference_candidate_label": all(
            set(item["row"]) == {"reference", "candidate", "label"} for item in serialized
        ),
        "all_rows_kernel_and_meta_checked_at_generation": unchecked == 0,
        "zero_conflicting_pairs": outcome.conflict_count == 0,
        "labels_balanced": manifest["labels"]["positive"] == manifest["labels"]["negative"],
        "exact_half_orientation": swapped * 2 == total,
        "one_swapped_row_per_root": all(count == 1 for count in per_root_swaps.values())
        and len(per_root_swaps) == len(roots),
        "finalized_shards_complete": all(bool(s["complete"]) for s in manifest["shards"]),
        "negative_mechanism_diversity": len(qualifying) >= 2,
        "candidate_only_upper_below_0_60": screen_by_name["candidate_only"]["upper_bound_95"]
        < 0.60,
        "reference_only_upper_below_0_60": screen_by_name["reference_only"]["upper_bound_95"]
        < 0.60,
        "family_held_out_upper_below_0_65": screen_by_name["family_held_out"]["upper_bound_95"]
        < 0.65,
    }
    report = {
        "schema_version": 2,
        "view": label,
        "generated_at": utc_now(),
        "source_runs": list(run_ids),
        "evaluated_on": "serialized_shards",
        "joined": {k: v for k, v in joined.items() if k != "core_report"},
        "core_report": core_report,
        "rows": total,
        "labels": manifest["labels"],
        "roots": len(roots),
        "orientation": {"swapped": swapped, "original": total - swapped},
        "negative_mechanism_diversity": {
            "rule": "at least two negative mechanisms with at least min(100, ceil(0.05 * rows))",
            "floor": floor,
            "counts": negatives_by_mechanism,
            "countable": countable,
            "exploratory_excluded": sorted(mechanism_of(op) for op in EXPLORATORY_NEGATIVES),
            "qualifying": qualifying,
        },
        "unchecked_rows": unchecked,
        "shortcut": screens,
        "proof_check_time": "original_generation",
        "replay_semantics": "journal_and_cache_replay_of_stored_terminals_no_fresh_kernel_replay",
        "checks": checks,
        "passed": all(checks.values()),
    }
    write_atomic(out / "release_report.json", canonical_json_bytes(report) + b"\n")
    status = (
        "seed_release_high_confidence" if report["passed"] else "candidate_seed_release_gate_failed"
    )
    manifest["artifact_status"] = status
    write_atomic(out / "manifest.json", canonical_json_bytes(manifest) + b"\n")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=find_repo_root(Path.cwd()))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--runs", default="tenk,v2_ne,v2_lt")
    parser.add_argument("--label", default="core_v2_seed")
    parser.add_argument("--n31-cap", type=float, default=0.02)
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    loaded = load_sprint_config(repo_root, args.config.resolve() if args.config else None)
    report = build_seed(
        repo_root,
        loaded,
        run_ids=tuple(args.runs.split(",")),
        label=args.label,
        n31_cap_fraction=args.n31_cap,
    )
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

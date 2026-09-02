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
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.paths import find_repo_root
from leanfaith.schemas.ids import PAIR_PREFIX, make_id
from leanfaith.sft1.sprint.engine import NEGATIVE_OPERATIONS, POSITIVE_OPERATIONS
from leanfaith.sft1.sprint.provenance import derive_provenance
from leanfaith.sft1.sprint.screens import residue_violation, unordered_pair_key
from leanfaith.sft1.sprint.store import read_json_object, write_atomic

ROW_FIELDS = {"pair_id", "root_id", "reference", "candidate", "label", "operation_id"}
VIEW_SIDECAR_FIELDS = {"orientation", "core_family", "core_cell"}


def _without_view_fields(sidecar: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in sidecar.items() if k not in VIEW_SIDECAR_FIELDS}


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


def _check_evidence(sidecar: Mapping[str, Any], operation: str) -> str | None:
    evidence = sidecar.get("evidence") or {}
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


def validate_view(
    *,
    repo_root: Path,
    staging_root: Path,
    run_id: str,
    compacted_dir: Path,
    retained_path: Path | None = None,
    retained_paths: Sequence[Path] = (),
    source_runs: Sequence[str] = (),
) -> dict[str, Any]:
    issues: list[str] = []
    counts: dict[str, int] = {}

    def issue(text: str) -> None:
        if len(issues) < 200:
            issues.append(text)
        counts[text.split(":", 1)[0]] = counts.get(text.split(":", 1)[0], 0) + 1

    manifest = read_json_object(compacted_dir / "manifest.json")
    sources = list(retained_paths)
    if retained_path is not None:
        sources.append(retained_path)
    retained: dict[str, dict[str, Any]] = {}
    for path in sources:
        for item in read_jsonl(path):
            retained.setdefault(str(item["row"]["pair_id"]), item)
    shard_dirs = sorted(compacted_dir.glob("shard-*"))
    total_rows = 0
    seen_pairs: set[str] = set()
    seen_keys: dict[str, str] = {}
    records: list[dict[str, Any]] = []
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
            pair_id = str(row.get("pair_id"))
            if set(row) != ROW_FIELDS:
                issue(f"row_schema: {pair_id}")
            if sidecar.get("pair_id") != pair_id:
                issue(f"row_sidecar_join: {pair_id}")
                continue
            if pair_id in seen_pairs:
                issue(f"duplicate_pair_id: {pair_id}")
            seen_pairs.add(pair_id)
            operation = str(row["operation_id"])
            label = bool(row["label"])
            expected_label = operation in POSITIVE_OPERATIONS or (
                operation.startswith("P") and operation not in NEGATIVE_OPERATIONS
            )
            if label != expected_label or bool(sidecar.get("label")) != label:
                issue(f"label_polarity: {pair_id}")
            if sidecar.get("root_id") != row["root_id"] or sidecar.get("operation_id") != operation:
                issue(f"sidecar_identity: {pair_id}")
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
            expected_pair = make_id(
                PAIR_PREFIX,
                {
                    "root_id": row["root_id"],
                    "operation_id": operation,
                    "reference_expr_hash": (reference.get("provenance") or {}).get("expr_hash"),
                    "candidate_expr_hash": (candidate.get("provenance") or {}).get("expr_hash"),
                },
            )
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
            source_record = retained.get(pair_id)
            if source_record is None:
                issue(f"missing_from_retained: {pair_id}")
            else:
                stored = _without_view_fields(sidecar)
                original = _without_view_fields(source_record["sidecar"])
                if original != stored:
                    issue(f"retained_record_mismatch: {pair_id}")
                if hash_canonical(original) != hash_canonical(stored):
                    issue(f"sidecar_hash_mismatch: {pair_id}")
                if sidecar.get("orientation") != "swapped" and source_record["row"] != row:
                    issue(f"retained_row_mismatch: {pair_id}")
                if sidecar.get("orientation") == "swapped" and (
                    source_record["row"]["reference"] != row["candidate"]
                    or source_record["row"]["candidate"] != row["reference"]
                ):
                    issue(f"orientation_swap_mismatch: {pair_id}")
            records.append({"row": row, "sidecar": sidecar})
    if total_rows != int(manifest.get("retained_rows", -1)):
        issue("shard_conservation: total shard rows differ from manifest retained_rows")
    conservation = (
        int(manifest.get("input_records", 0))
        - sum(int(v) for v in (manifest.get("screen_rejections") or {}).values())
        - int(manifest.get("duplicates_removed", 0))
        - int(manifest.get("conflicting_rows_rejected", 0))
        - int(manifest.get("view_dropped", 0))
    )
    if conservation != int(manifest.get("retained_rows", -1)):
        issue("manifest_conservation: input - rejections - duplicates - conflicts - view drop")
    for source_run in list(source_runs) or [run_id]:
        run_dir = staging_root / "runs" / source_run
        status_path = run_dir / "status.json"
        status = read_json_object(status_path) if status_path.is_file() else {}
        if status.get("final") is not True:
            issue(f"run_status: {source_run} status.json is not final")
        replay_path = run_dir / "replay_report.json"
        replay = read_json_object(replay_path) if replay_path.is_file() else None
        if replay is None:
            issue(f"replay_receipt: {source_run} missing replay_report.json")
        elif replay.get("lean_requests") != 0 or replay.get("duplicate_rows") != 0:
            issue(f"replay_receipt: {source_run} replay issued Lean requests or appended rows")
    provenance = derive_provenance(records, repo_root=repo_root, cache_root=staging_root / "cache")
    if not provenance["consistent"]:
        for text in provenance["issues"]:
            issue(f"provenance: {text}")
    manifest_provenance = manifest.get("provenance")
    if not isinstance(manifest_provenance, dict):
        issue("manifest_provenance: manifest lacks sidecar-derived provenance")
    else:
        recorded = {
            (s["engine_source_sha256"], s["compile_context_id"], s["rows"])
            for s in manifest_provenance.get("segments", [])
        }
        derived = {
            (s["engine_source_sha256"], s["compile_context_id"], s["rows"])
            for s in provenance["segments"]
        }
        if recorded != derived:
            issue("manifest_provenance: recorded segments differ from sidecar-derived segments")
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "compacted_dir": str(compacted_dir),
        "rows_checked": total_rows,
        "shards": len(shard_dirs),
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
        source_runs = [str(item) for item in manifest.get("source_runs", [])]
        report = validate_view(
            repo_root=repo_root,
            staging_root=staging,
            run_id=args.label,
            compacted_dir=compacted,
            retained_paths=[RunPaths(staging, run).retained for run in source_runs],
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

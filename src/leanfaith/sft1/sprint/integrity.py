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
from leanfaith.sft1.sprint.engine import NEGATIVE_OPERATIONS, POSITIVE_OPERATIONS, mechanism_of
from leanfaith.sft1.sprint.provenance import derive_provenance
from leanfaith.sft1.sprint.screens import residue_violation, unordered_pair_key
from leanfaith.sft1.sprint.store import read_json_object, write_atomic

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
}


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


def sidecar_aggregate_counts(sidecars: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Recompute every manifest aggregate from the finalized sidecars of a view."""
    counts: dict[str, dict[str, int]] = {
        "operations": {},
        "mechanisms": {},
        "negative_mechanisms": {},
        "transforms": {},
        "families": {},
        "row_kinds": {},
    }
    roots: set[str] = set()
    squares: set[str] = set()
    positives = 0
    for sidecar in sidecars:
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
            counts[name][key] = counts[name].get(key, 0) + 1
        roots.add(str(sidecar.get("root_id")))
        squares.add(f"{sidecar.get('root_id')}|{sidecar.get('operation_id')}")
        positives += 1 if bool(sidecar.get("label")) else 0
    return {
        **{name: dict(sorted(values.items())) for name, values in counts.items()},
        "roots": len(roots),
        "squares": len(squares),
        "retained_rows": len(sidecars),
        "labels": {"positive": positives, "negative": len(sidecars) - positives},
        "curriculum_only": any(
            str(sidecar.get("operation_id")) == "SQUARE_N19_CURRICULUM_V1" for sidecar in sidecars
        ),
    }


def manifest_aggregate_issues(
    manifest: Mapping[str, Any], sidecars: Sequence[Mapping[str, Any]]
) -> list[str]:
    """Every aggregate the manifest reports must equal the sidecar-derived value.

    Square manifests (those with ``row_kinds``) are checked for all aggregates; other
    manifests only for the aggregates they carry.
    """
    if not sidecars:
        return []
    derived = sidecar_aggregate_counts(sidecars)
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
    recorded_sources = manifest.get("source_retained_paths")
    if isinstance(recorded_sources, list) and recorded_sources:
        # regenerated views name the exact retained files they were built from
        sources = [staging_root / str(item) for item in recorded_sources]
        for path in sources:
            if not path.is_file():
                issue(f"source_retained_missing: {path}")
    # pair id -> stored (sidecar hash without view fields, model row) copies; hashing keeps
    # memory flat for views with hundreds of thousands of rows
    retained: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for path in sources:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                item = json.loads(line)
                retained.setdefault(_pair_id(item), []).append(
                    (hash_canonical(_without_view_fields(item["sidecar"])), dict(item["row"]))
                )
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
            records.append({"row": row, "sidecar": _slim_for_provenance(sidecar)})
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
    aggregate_issues = manifest_aggregate_issues(manifest, [r["sidecar"] for r in records])
    for text in aggregate_issues:
        issue(f"manifest_aggregate: {text}")
    provenance = derive_provenance(
        records,
        repo_root=repo_root,
        cache_root=staging_root / "cache",
        release_dir=compacted_dir,
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

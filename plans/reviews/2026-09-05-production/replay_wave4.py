"""Replay Wave 4 selection from a compact Git-only evidence projection.

Default execution reads only this checkout. ``--extract-from-storage`` rebuilds
the projection from the 13 original public-library runs and is packaging-only.
This is a selection diagnostic, not a certificate verifier or release builder.
"""

from __future__ import annotations

import argparse
import copy
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from leanfaith.config.hashing import hash_canonical, hash_file
from leanfaith.sft1.sprint.orbit import OrbitError
from leanfaith.sft1.sprint.screens import render_hash, unordered_pair_key
from leanfaith.sft1.sprint.square import (
    _balance_wave4_pair_delta_units,
    _wave4_row_hash,
    load_wave4_retained_dir,
    materialize_wave4_records,
    select_wave4_release_groups,
)
from leanfaith.sft1.sprint.views import wave3_pair_delta

HERE = Path(__file__).resolve().parent
DATA = HERE / "wave4"
SOURCE = Path("/storage/milikic/leanfaith/value_first/sft1_deterministic_v1/wave4")
BASE = "control/gate-inputs-bec8f12-v2/run_summary.json"
SUPPLEMENT = "control/supplement-bec8f12-v4/terminal.json"
SIDECAR_FIELDS = (
    "pair_id",
    "root_id",
    "row_kind",
    "operation_id",
    "negative_operation",
    "closure_group_ids",
    "evidence_hash",
)
SALT = "git-only-production-review-2026-09-05"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def source_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path.relative_to(SOURCE)), "absent": True}
    return {
        "path": str(path.relative_to(SOURCE)),
        "sha256": hash_file(path),
        "bytes": path.stat().st_size,
    }


def pack_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    """Intern repeated structured objects losslessly; keep all scalar text readable."""
    counts: Counter[str] = Counter()
    objects: dict[str, Any] = {}

    def visit(value: Any) -> None:
        if isinstance(value, (dict, list)):
            digest = hash_canonical(value)
            counts[digest] += 1
            objects[digest] = value
            for child in value.values() if isinstance(value, dict) else value:
                visit(child)

    for value in evidence.values():
        visit(value)
    shared = {
        digest: index
        for index, digest in enumerate(
            sorted(
                digest
                for digest, count in counts.items()
                if count > 1 and len(json.dumps(objects[digest])) > 100
            )
        )
    }

    def encode(value: Any, *, outer: bool = False) -> Any:
        if isinstance(value, (dict, list)):
            digest = hash_canonical(value)
            if not outer and digest in shared:
                return {"$shared": shared[digest]}
            if isinstance(value, dict):
                return {key: encode(child) for key, child in value.items()}
            return [encode(child) for child in value]
        return value

    return {
        "encoding": "lossless_repeated_object_table_v1",
        "shared": [encode(objects[digest], outer=True) for digest in shared],
        "objects": {digest: encode(value) for digest, value in evidence.items()},
    }


def unpack_evidence(packed: dict[str, Any]) -> dict[str, Any]:
    def decode(value: Any) -> Any:
        if isinstance(value, dict):
            if set(value) == {"$shared"}:
                return decode(packed["shared"][value["$shared"]])
            return {key: decode(child) for key, child in value.items()}
        if isinstance(value, list):
            return [decode(child) for child in value]
        return value

    return {digest: decode(value) for digest, value in packed["objects"].items()}


def extract() -> None:
    """Project exact original data; no fields affecting selection are invented."""
    DATA.mkdir(parents=True, exist_ok=True)
    base = read_json(SOURCE / BASE)
    supplement = read_json(SOURCE / SUPPLEMENT)
    runs = [Path(run["run_dir"]) for run in base["runs"]]
    runs += [SOURCE / "mathlib" / "runs" / run["run_id"] for run in supplement["supplement_runs"]]
    files = [source_file(SOURCE / BASE), source_file(SOURCE / SUPPLEMENT)]
    texts: list[str] = []
    text_ids: dict[str, int] = {}
    evidence: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    run_reports = []
    for run in runs:
        manifest = read_json(run / "run.json")
        status = read_json(run / "status.json")
        replay = read_json(run / "replay_report.json")
        project = manifest["project"]["project_id"]
        assert project in {"mathlib", "physlib", "cslib"}, project
        assert status["final"] is True and replay["lean_requests"] == 0
        original = load_wave4_retained_dir(run)
        assert len(original.rows) == status["physical_rows"]
        assert len(original.groups) == status["retained_variants"]
        files += [
            source_file(run / name)
            for name in (
                "run.json",
                "status.json",
                "replay_report.json",
                "journal.jsonl",
                "retained.jsonl",
            )
        ]
        run_reports.append(
            {
                "run_id": run.name,
                "project_id": project,
                "project_revision": manifest["project"]["project_revision"],
                "implementation_commit": manifest["implementation_commit"],
                "rows": len(original.rows),
                "groups": len(original.groups),
                "roots": len({group.root_id for group in original.groups}),
                "original_materialization_passed": True,
                "final_status": True,
                "reported_replay_lean_requests": 0,
            }
        )
        for record in original.rows:
            sidecar = record["sidecar"]
            projected = {field: sidecar[field] for field in SIDECAR_FIELDS}
            projected["project"] = {"project_id": project}
            selected_evidence = {}
            for field in ("negative_family_evidence", "negative_last_replay"):
                if field in sidecar["evidence"]:
                    value = sidecar["evidence"][field]
                    digest = hash_canonical(value)
                    evidence[digest] = value
                    selected_evidence[field] = digest
            projected["evidence_refs"] = selected_evidence
            model_row = dict(record["row"])
            for field in ("reference", "candidate"):
                value = model_row[field]
                if value not in text_ids:
                    text_ids[value] = len(texts)
                    texts.append(value)
                model_row[field] = text_ids[value]
            rows.append(
                {
                    "row": model_row,
                    "sidecar": projected,
                    "row_hash": record["row_hash"],
                    "unordered_pair_key": record["unordered_pair_key"],
                    "source_run": run.name,
                }
            )
        groups.extend(group.record for group in original.groups)
    write_json(DATA / "texts.json", texts)
    packed = pack_evidence(evidence)
    assert unpack_evidence(packed) == evidence
    write_json(DATA / "evidence_objects.json", packed)
    for filename, values in (("rows.jsonl", rows), ("groups.jsonl", groups)):
        (DATA / filename).write_text(
            "".join(
                json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n" for value in values
            )
        )
    write_json(
        DATA / "sources.json",
        {
            "schema": "wave4_selection_projection_v1",
            "scope": "all terminal-authorized rows/groups from 11 base and 2 supplement runs",
            "not_sampled": True,
            "run_count": len(runs),
            "runs": run_reports,
            "original_files": files,
            "included_sidecar_fields": [*SIDECAR_FIELDS, "project.project_id"],
            "included_evidence_subobjects": ["negative_family_evidence", "negative_last_replay"],
            "omitted": [
                "full proof/certificate sidecars",
                "cache payloads",
                "full journals",
                "representation provenance",
                "all compiler-source data",
            ],
            "limits": (
                "Original evidence_hash values are retained as opaque bindings. Only the two "
                "negative evidence subobjects consumed by materialize are included and hashed. "
                "The full sidecar hash and Lean proofs cannot be verified from this projection. "
                "Original file SHA256 values identify off-Git inputs; they do not make those "
                "inputs available to a Git-only reviewer. No release approval follows from replay."
            ),
            "projection_files": {
                name: {"sha256": hash_file(DATA / name), "bytes": (DATA / name).stat().st_size}
                for name in ("texts.json", "evidence_objects.json", "rows.jsonl", "groups.jsonl")
            },
        },
    )


def load_projection() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    sources = read_json(DATA / "sources.json")
    for name, expected in sources["projection_files"].items():
        assert hash_file(DATA / name) == expected["sha256"], name
    texts = read_json(DATA / "texts.json")
    evidence = unpack_evidence(read_json(DATA / "evidence_objects.json"))
    for digest, value in evidence.items():
        assert hash_canonical(value) == digest
    rows = [json.loads(line) for line in (DATA / "rows.jsonl").read_text().splitlines()]
    groups = [json.loads(line) for line in (DATA / "groups.jsonl").read_text().splitlines()]
    for record in rows:
        for field in ("reference", "candidate"):
            record["row"][field] = texts[record["row"][field]]
        sidecar = record["sidecar"]
        sidecar["evidence"] = {
            field: evidence[digest] for field, digest in sidecar.pop("evidence_refs").items()
        }
        row = record["row"]
        assert (
            unordered_pair_key(render_hash(row["reference"]), render_hash(row["candidate"]))
            == record["unordered_pair_key"]
        )
        assert _wave4_row_hash(row, sidecar) == record["row_hash"]
    return rows, groups


def stats(materialized: Any) -> dict[str, Any]:
    return {
        "rows": len(materialized.rows),
        "groups": len(materialized.groups),
        "roots": len({group.root_id for group in materialized.groups}),
        "projects": dict(
            sorted(
                Counter(
                    row["sidecar"]["project"]["project_id"] for row in materialized.rows
                ).items()
            )
        ),
        "labels": dict(
            sorted(
                Counter(
                    "positive" if row["row"]["label"] else "negative" for row in materialized.rows
                ).items()
            )
        ),
    }


def replay() -> dict[str, Any]:
    rows, groups = load_projection()
    by_pair = {row["sidecar"]["pair_id"]: row for row in rows}
    assert len(by_pair) == len(rows)
    duplicates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in rows:
        duplicates[record["unordered_pair_key"]].append(record)
    duplicate_classes = [values for _, values in sorted(duplicates.items()) if len(values) > 1]
    label_conflicts = sum(
        len({row["row"]["label"] for row in values}) > 1 for values in duplicate_classes
    )
    try:
        materialize_wave4_records(rows, groups)
    except OrbitError as error:
        unfiltered_error = str(error)
    else:
        raise AssertionError("Expected the duplicate-pair guard to reject the raw union")

    # Proposed diagnostic only: stable greedy whole-group collision removal.
    # It never relabels, rewrites model text, or retains a partial logical group.
    owners: dict[str, str] = {}
    selected = []
    dropped = []
    for group in sorted(groups, key=lambda value: value["group_id"]):
        pairs = list(group["logical_pair_ids"].values())
        conflicts = [
            pair
            for pair in pairs
            if (
                by_pair[pair]["unordered_pair_key"] in owners
                and owners[by_pair[pair]["unordered_pair_key"]] != pair
            )
        ]
        if conflicts:
            dropped.append({"group_id": group["group_id"], "conflicting_pair_ids": conflicts})
            continue
        selected.append(group)
        for pair in pairs:
            owners[by_pair[pair]["unordered_pair_key"]] = pair
    memberships: dict[str, list[str]] = defaultdict(list)
    for group in selected:
        for pair in group["logical_pair_ids"].values():
            memberships[pair].append(group["group_id"])
    retained = []
    for pair, member_groups in sorted(memberships.items()):
        record = copy.deepcopy(by_pair[pair])
        record["sidecar"]["closure_group_ids"] = sorted(member_groups)
        record["row_hash"] = _wave4_row_hash(record["row"], record["sidecar"])
        retained.append(record)
    materialized = materialize_wave4_records(retained, selected)
    balanced, balance_report = _balance_wave4_pair_delta_units(materialized, selection_salt=SALT)
    selection_plain = select_wave4_release_groups(
        materialized,
        maximum_rows=None,
        n25_maximum_share=0.25,
        selection_salt=SALT,
        enforce_pair_delta_balance=False,
    )
    selection_balanced = select_wave4_release_groups(
        materialized,
        maximum_rows=None,
        n25_maximum_share=0.25,
        selection_salt=SALT,
        enforce_pair_delta_balance=True,
    )
    roots: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "positive": 0,
            "negative": 0,
            "cell_deltas": Counter(),
        }
    )
    for record in materialized.rows:
        root = roots[record["sidecar"]["root_id"]]
        positive = record["row"]["label"]
        root["positive" if positive else "negative"] += 1
        root["cell_deltas"][wave3_pair_delta(record)["cell"]] += 1 if positive else -1
    return {
        "kind": "git_only_wave4_selection_replay_v1",
        "input": {
            "runs": 13,
            "rows": len(rows),
            "groups": len(groups),
            "roots": len({group["root_id"] for group in groups}),
        },
        "duplicates": {
            "classes": len(duplicate_classes),
            "rows_in_classes": sum(map(len, duplicate_classes)),
            "label_conflicts": label_conflicts,
            "unfiltered_production_materialize_error": unfiltered_error,
            "class_members": [
                [
                    {
                        "pair_id": row["sidecar"]["pair_id"],
                        "root_id": row["sidecar"]["root_id"],
                        "label": row["row"]["label"],
                        "source_run": row["source_run"],
                    }
                    for row in values
                ]
                for values in duplicate_classes
            ],
        },
        "diagnostic_whole_group_dedup": {
            "policy": "ascending_group_id_greedy_keep_first_physical_pair_owner",
            "dropped_groups": dropped,
            "result": stats(materialized),
        },
        "current_balance_function": stats(balanced),
        "current_release_selection_without_balance": stats(selection_plain.materialized),
        "current_release_selection_with_balance": stats(selection_balanced.materialized),
        "root_imbalance_diagnostic": {
            "positive_excess_roots": sum(r["positive"] > r["negative"] for r in roots.values()),
            "negative_excess_roots": sum(r["positive"] < r["negative"] for r in roots.values()),
            "equal_total_label_roots": sum(r["positive"] == r["negative"] for r in roots.values()),
            "exact_zero_cell_vector_roots": sum(
                not any(r["cell_deltas"].values()) for r in roots.values()
            ),
            "interpretation": (
                "A base with n variants contributes 2n positive versus n+1 negative physical "
                "rows. Shared negatives therefore create positive excess for n>1. The "
                "current function retains only zero cell vectors or exact inverse-vector "
                "pairs; aggregate balance alone is insufficient."
            ),
        },
        "balance_report": balance_report,
        "scope_limits": (
            "All 202 roots are analyzed before an exact-200 selection. This replay does not "
            "execute that complete gate, gold screening, proof verification, manual inspection, "
            "publication, training, or Lean. Dedup is a diagnostic proposal, not production code."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extract-from-storage", action="store_true")
    parser.add_argument("--write-expected", action="store_true")
    args = parser.parse_args()
    if args.extract_from_storage:
        extract()
    report = replay()
    if args.write_expected:
        write_json(DATA / "expected_report.json", report)
    else:
        assert report == read_json(DATA / "expected_report.json"), "replay report drifted"
    print(
        json.dumps(
            {
                key: value
                for key, value in report.items()
                if key not in {"balance_report", "duplicates", "diagnostic_whole_group_dedup"}
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print("PASS: Git-only replay matches expected_report.json")


if __name__ == "__main__":
    main()

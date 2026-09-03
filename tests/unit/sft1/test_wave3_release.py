from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.schemas.ids import PAIR_PREFIX, make_id
from leanfaith.sft1.sprint.engine import mechanism_of
from leanfaith.sft1.sprint.integrity import validate_view
from leanfaith.sft1.sprint.publish import local_files
from leanfaith.sft1.sprint.screens import render_hash, unordered_pair_key
from leanfaith.sft1.sprint.store import SemanticCache, read_json_object
from leanfaith.sft1.sprint.views import (
    WAVE3_GATE_OPERATIONS,
    ViewError,
    _inspection_receipts,
    _load_completed_run,
    _mixed_200_gate_receipt,
    build_wave3_release,
    main,
    write_wave3_family_gate_receipt,
)

ROOT = Path(__file__).resolve().parents[3]
GOLD = ROOT / "data/benchmarks/golden_blocklist_v1.json"
CHECKED = {"meta_checked": True, "kernel_checked": True}
ENGINE_PATH = "LeanFaith/Meta/SFT1/Sprint.lean"
ENGINE_SOURCE_SHA256 = sha256_hex(
    subprocess.run(
        ["git", "-C", str(ROOT), "show", f"HEAD:{ENGINE_PATH}"],
        check=True,
        capture_output=True,
    ).stdout
)
IMPLEMENTATION_COMMIT = subprocess.run(
    ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
REPR_IDENTITY = {"renderer": "synthetic-goal-v1", "revision": 1}
REPR_SPEC_HASH = hash_canonical(["synthetic-goal-v1-spec", 1])
OPERATIONS = (
    "P18_SYMMETRIZE_EQUALITY_V1",
    "P_ORDER_COMPLEMENT_V1",
    "N32_SWAP_ROLE_ORDER_PROOF_V1",
    "N25_TOGGLE_EQ_NE_PROOF_V1",
)


@pytest.fixture(autouse=True)
def _clean_release_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "leanfaith.sft1.sprint.views._git_identity",
        lambda _root: {
            "commit": IMPLEMENTATION_COMMIT,
            "dirty": False,
            "views_source_sha256": hash_file(ROOT / "src/leanfaith/sft1/sprint/views.py"),
        },
    )


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def _goals(operation: str, root: str, *, conflict: bool) -> tuple[str, str]:
    context = f"x y : Nat\nh : x = x -- {root}\n⊢ "
    reference = context + "x = y"
    if operation == "P18_SYMMETRIZE_EQUALITY_V1":
        return reference, context + "y = x"
    if operation == "P_ORDER_COMPLEMENT_V1":
        return reference, context + "x ≤ y"
    if operation == "N32_SWAP_ROLE_ORDER_PROOF_V1":
        if conflict:
            return reference, context + "y = x"
        return reference, context + "x = x"
    return reference, context + "x ≠ y"


def _evidence(operation: str) -> dict[str, object]:
    if operation.startswith("P"):
        return {
            "candidate_truth": "proved_equivalent_to_reference",
            "equivalence_proof": {"check": CHECKED},
        }
    refutation: dict[str, object] = {"check": CHECKED}
    if operation in {
        "N26_INCREMENT_BOUND_PROOF_V1",
        "N31_DROP_REQUIRED_GUARD_PROOF_V1",
    }:
        refutation["separator"] = {"check": CHECKED}
    elif operation == "N30_ADD_UNJUSTIFIED_UNIQUENESS_PROOF_V1":
        refutation.update(
            {
                "witnesses": ["0", "1"],
                "witness_checks": [CHECKED, CHECKED, CHECKED],
                "enumeration": "finite_complete",
            }
        )
    elif operation == "N29_SWAP_WITNESS_DEPENDENCY_PROOF_V1":
        refutation.update(
            {
                "witness_checks": [CHECKED],
                "enumeration": "finite_complete_matrix",
            }
        )
    return {
        "candidate_truth": "refuted",
        "source_proof_check": CHECKED,
        "refutation": refutation,
    }


def _record(
    project: str,
    revision: str,
    root: str,
    operation: str,
    engine: dict[str, str],
    cache_root: Path,
    *,
    conflict: bool = False,
) -> dict[str, Any]:
    reference, candidate = _goals(operation, root, conflict=conflict)
    reference_expr_hash = hash_canonical([project, root, "reference", reference])
    candidate_expr_hash = hash_canonical([project, root, operation, "candidate", candidate])
    root_id = "root:" + hash_canonical([project, revision, root])
    pair_id = make_id(
        PAIR_PREFIX,
        {
            "root_id": root_id,
            "operation_id": operation,
            "reference_expr_hash": reference_expr_hash,
            "candidate_expr_hash": candidate_expr_hash,
        },
    )
    label = operation.startswith("P")
    evidence = _evidence(operation)
    reference_hash = render_hash(reference)
    candidate_hash = render_hash(candidate)
    row_hash = hash_canonical([pair_id, evidence, "row"])
    project_identity = {
        "project_id": project,
        "project_dir": f"/synthetic/{project}",
        "project_revision": revision,
        "lean_version": "v4.31.0-rc1",
        "lean_interact_version": "0.11.4",
        "repl_revision": "synthetic-repl-revision",
        "import_header": f"import {project.title()}",
        "options": {"Elab.async": False, "autoImplicit": False},
    }
    reference_record = {
        "goal_v1": reference,
        "rendered_goal_hash": reference_hash,
        "implementation_identity": REPR_IDENTITY,
        "spec_hash": REPR_SPEC_HASH,
        "provenance": {"expr_hash": reference_expr_hash},
    }
    candidate_record = {
        "goal_v1": candidate,
        "rendered_goal_hash": candidate_hash,
        "implementation_identity": REPR_IDENTITY,
        "spec_hash": REPR_SPEC_HASH,
        "provenance": {"expr_hash": candidate_expr_hash},
    }
    reference_source_material = {"kind": "synthetic", "root": root}
    candidate_source_material = {"kind": "constructed", "operation": operation}
    reference_alpha_hash = hash_canonical([project, revision, root, reference, "alpha"])
    root_key = SemanticCache.root_key(
        project_revision=revision,
        lean_version="v4.31.0-rc1",
        import_options_fingerprint=engine["import_options_fingerprint"],
        engine_semantic_version=engine["semantic_version"],
        name=root,
        engine_source_sha256=engine["source_sha256"],
    )
    operation_key = SemanticCache.op_key(
        reference_alpha_hash=reference_alpha_hash,
        operation_id=operation,
        engine_semantic_version=engine["semantic_version"],
        lean_version="v4.31.0-rc1",
        project_revision=revision,
        import_options_fingerprint=engine["import_options_fingerprint"],
        name=root,
        engine_source_sha256=engine["source_sha256"],
    )
    process_hash = hash_canonical([project, root, operation, "process"])
    render_request_hash = hash_canonical([project, root, operation, "render"])
    sidecar = {
        "pair_id": pair_id,
        "root_id": root_id,
        "root_name": root,
        "operation_id": operation,
        "mechanism": mechanism_of(operation),
        "label": label,
        "engine": engine,
        "project": project_identity,
        "implementation_commit": IMPLEMENTATION_COMMIT,
        "runner_source_sha256": hash_canonical(["synthetic-runner"]),
        "cache_schema": 3,
        "cache_key": operation_key,
        "lean_request_hashes": {"process": process_hash, "render": render_request_hash},
        "site": {"kind": "synthetic", "detail": operation},
        "evidence": evidence,
        "evidence_hash": hash_canonical(evidence),
        "repr": {
            "reference": reference_record,
            "reference_source_material": reference_source_material,
            "candidate": candidate_record,
            "candidate_source_material": candidate_source_material,
        },
    }
    cache = SemanticCache(cache_root)
    root_record = cache.get_root(root_key) or {
        "schema_version": 1,
        "name": root,
        "project_revision": revision,
        "lean_version": project_identity["lean_version"],
        "engine": engine,
        "reference_goal": reference,
        "reference_alpha_hash": reference_alpha_hash,
        "root_status": "ok",
        "ops": {},
    }
    root_record["ops"][operation] = operation_key
    cache.put_root(root_key, root_record)
    cache.put_op(
        operation_key,
        {
            "schema_version": 1,
            "root": root,
            "operation_id": operation,
            "label": label,
            "status": "retained",
            "engine": engine,
            "site": sidecar["site"],
            "evidence": evidence,
            "candidate_goal": candidate,
            "process_request_hash": process_hash,
            "render": {
                "request_hash": render_request_hash,
                "reference": {
                    "record": reference_record,
                    "source_material": reference_source_material,
                },
                "candidate": {
                    "record": candidate_record,
                    "source_material": candidate_source_material,
                },
            },
        },
    )
    return {
        "row": {
            "pair_id": pair_id,
            "root_id": root_id,
            "reference": reference,
            "candidate": candidate,
            "label": label,
            "operation_id": operation,
        },
        "sidecar": sidecar,
        "row_hash": row_hash,
        "unordered_pair_key": unordered_pair_key(reference_hash, candidate_hash),
        "label": label,
        "operation_id": operation,
        "root_name": root,
    }


def _write_run(
    run_dir: Path,
    project: str,
    root_count: int,
    *,
    resumed: bool = False,
    conflict: bool = False,
    incomplete: bool = False,
    unauthorized: bool = False,
    run_tag: str = "",
    operations: Sequence[str] = OPERATIONS,
) -> None:
    revision = f"{project}-revision"
    run_id = f"wave3-{project}{run_tag}"
    roots = [f"{project}{run_tag}.root{index:03d}" for index in range(root_count)]
    engine = {
        "semantic_version": "sft1_wave3_engine_v1",
        "source_sha256": ENGINE_SOURCE_SHA256,
        "compile_context_id": "ctx:" + hash_canonical([project, revision, "context"]),
        "import_options_fingerprint": hash_canonical([project, "import-options"]),
    }
    records = [
        _record(
            project,
            revision,
            root,
            operation,
            engine,
            run_dir.parent.parent / "cache",
            conflict=conflict and root == roots[0] and operation == "N32_SWAP_ROLE_ORDER_PROOF_V1",
        )
        for root in roots
        for operation in operations
    ]
    manifest = {
        "schema_version": 1,
        "sprint_id": f"sft1-wave3-{project}",
        "run_id": run_id,
        "project": {
            "project_id": project,
            "project_dir": f"/synthetic/{project}",
            "project_revision": revision,
            "lean_version": "v4.31.0-rc1",
            "lean_interact_version": "0.11.4",
            "repl_revision": "synthetic-repl-revision",
            "import_header": f"import {project.title()}",
            "options": {"Elab.async": False, "autoImplicit": False},
        },
        "engine": engine,
        "operations": list(operations),
        "explicit_roots": roots,
        "explicit_roots_sha256": hash_canonical(roots),
        "roots_file_identity": {
            "file_sha256": hash_canonical([project, "file"]),
            "metadata_sha256": hash_canonical([project, "metadata"]),
            "project_id": project,
            "project_revision": revision,
            "inventory_sha256": hash_canonical([project, "inventory"]),
            "count": len(roots),
            "roots_sha256": hash_canonical(roots),
        },
        "gold_blocklist_sha256": hash_file(GOLD),
        "config_semantic_hash": hash_canonical(["config", project]),
        "implementation_commit": IMPLEMENTATION_COMMIT,
        "implementation_dirty": False,
        "runner_source_sha256": hash_canonical(["synthetic-runner"]),
    }
    status = {
        "run_id": run_id,
        "final": not incomplete,
        "roots_considered": len(roots),
        "roots_this_process": len(roots) - 1 if resumed else len(roots),
        "roots_lean": len(roots),
        "roots_cache": 0,
        "retained_total": len(records),
        "lean_requests": 1,
        "lean_elapsed_ms": 10,
        "wall_seconds": 0.02,
        "peak_process_tree_rss_bytes": 1024,
    }
    replay = {
        "run_id": run_id,
        "lean_requests": 0,
        "duplicate_rows": 0,
        "retained_before": len(records),
        "retained_after": len(records),
        "roots_considered": len(roots),
    }
    journal: list[dict[str, Any]] = []
    for record in records:
        journal.append(
            {
                "kind": "terminal",
                "root": record["root_name"],
                "operation_id": record["operation_id"],
                "status": "retained",
                "pair_id": record["row"]["pair_id"],
                "row_hash": record["row_hash"],
                "unordered_pair_key": record["unordered_pair_key"],
            }
        )
    if unauthorized:
        journal.pop()
    _write_json(run_dir / "run.json", manifest)
    _write_json(run_dir / "status.json", status)
    _write_json(run_dir / "replay_report.json", replay)
    (run_dir / "journal.jsonl").write_bytes(
        b"".join(canonical_json_bytes(item) + b"\n" for item in journal)
    )
    (run_dir / "retained.jsonl").write_bytes(
        b"".join(canonical_json_bytes(item) + b"\n" for item in records)
    )


def _runs(tmp_path: Path, *, conflict: bool = False) -> list[Path]:
    counts = {"mathlib": 3, "physlib": 2, "cslib": 2}
    runs: list[Path] = []
    for project, count in counts.items():
        run = tmp_path / "runs" / project
        _write_run(
            run,
            project,
            count,
            resumed=project == "mathlib",
            conflict=conflict and project == "cslib",
        )
        runs.append(run)
    return runs


def _tree(path: Path) -> dict[str, bytes]:
    return {
        str(item.relative_to(path)): item.read_bytes()
        for item in sorted(path.rglob("*"))
        if item.is_file()
    }


def _rows(output: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(output.glob("shard-*/rows.jsonl")):
        rows.extend(json.loads(line) for line in path.read_text("utf-8").splitlines())
    return rows


def _sidecars(output: Path) -> list[dict[str, Any]]:
    sidecars: list[dict[str, Any]] = []
    for path in sorted(output.glob("shard-*/sidecars.jsonl")):
        sidecars.extend(json.loads(line) for line in path.read_text("utf-8").splitlines())
    return sidecars


def _revalidate(output: Path) -> dict[str, Any]:
    manifest = read_json_object(output / "manifest.json")
    return validate_view(
        repo_root=ROOT,
        staging_root=output.parent,
        run_id=str(manifest["release_id"]),
        compacted_dir=output,
    )


def test_exact_schema_n25_cap_final_shard_and_deterministic_replay(tmp_path: Path) -> None:
    runs = _runs(tmp_path)
    outputs = [tmp_path / "release-a", tmp_path / "release-b"]
    reports = [
        build_wave3_release(
            repo_root=ROOT,
            run_dirs=list(reversed(runs)) if index else runs,
            output_dir=output,
            gold_blocklist_path=GOLD,
            shard_size=10,
        )
        for index, output in enumerate(outputs)
    ]

    assert _tree(outputs[0]) == _tree(outputs[1])
    assert reports[0] == reports[1]
    assert reports[0]["rows"] == 28
    assert reports[0]["n19_rows"] == 0
    assert reports[0]["n25_rows"] == 7
    assert reports[0]["n25_share"] == 0.25
    assert reports[0]["checks"]["pair_delta_cells_balanced"] is True
    assert reports[0]["checks"]["manual_inspection"] is False
    assert reports[0]["checks"]["integrity_report"] is True
    assert reports[0]["conservation"]["holds"] is True
    assert all(set(row) == {"reference", "candidate", "label"} for row in _rows(outputs[0]))
    manifest = read_json_object(outputs[0] / "manifest.json")
    integrity = read_json_object(outputs[0] / "integrity_report.json")
    assert integrity["passed"] is True
    assert integrity["issues"] == []
    assert integrity["source_retained_files_checked"] == 3
    assert manifest["provenance"]["project_pin_set_count"] == 3
    assert len(manifest["source_retained_files"]) == 3
    assert manifest["source_cache_snapshot"]["operation_records"] == reports[0]["rows"]
    assert all(
        set(sidecar["release"]["source_cache"]) == {"root", "operation"}
        for sidecar in _sidecars(outputs[0])
    )

    shard_manifests = [
        json.loads(path.read_text("utf-8"))
        for path in sorted(outputs[0].glob("shard-*/manifest.json"))
    ]
    assert [manifest["row_count"] for manifest in shard_manifests] == [12, 12, 4]
    assert all(manifest["complete"] and manifest["finalized"] for manifest in shard_manifests)


@pytest.mark.parametrize("defect", ["incomplete", "unauthorized"])
def test_partial_or_unauthorized_run_fails_closed(tmp_path: Path, defect: str) -> None:
    runs = _runs(tmp_path)
    _write_run(
        runs[0],
        "mathlib",
        3,
        incomplete=defect == "incomplete",
        unauthorized=defect == "unauthorized",
    )
    with pytest.raises(ViewError, match="not release-authorized"):
        build_wave3_release(
            repo_root=ROOT,
            run_dirs=runs,
            output_dir=tmp_path / "release",
            gold_blocklist_path=GOLD,
        )
    assert not (tmp_path / "release").exists()


def test_cross_source_conflicting_label_fails_before_output(tmp_path: Path) -> None:
    runs = _runs(tmp_path, conflict=True)
    with pytest.raises(ViewError, match="cross-source conflicting labels"):
        build_wave3_release(
            repo_root=ROOT,
            run_dirs=runs,
            output_dir=tmp_path / "release",
            gold_blocklist_path=GOLD,
        )
    assert not (tmp_path / "release").exists()


def test_wave3_release_cli_uses_explicit_run_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _runs(tmp_path)
    output = tmp_path / "cli-release"
    passing_screens = {
        "rows": 28,
        "screens": [
            {"name": "candidate_only", "passed": True},
            {"name": "reference_only", "passed": True},
            {"name": "family_held_out", "passed": True},
        ],
        "passed": True,
    }
    monkeypatch.setattr(
        "leanfaith.sft1.sprint.views._screen_results", lambda records: passing_screens
    )
    family_gates = {
        "provided": True,
        "receipts": [{"candidate_audit": {"passed": True}, "passed": True} for _ in range(5)],
        "required_operations": [],
        "exact_family_set": True,
        "useful_operations": ["N26", "N30", "N31"],
        "useful_family_count": 3,
        "passed": True,
    }
    monkeypatch.setattr(
        "leanfaith.sft1.sprint.views._inspection_receipts",
        lambda *args, **kwargs: family_gates,
    )
    monkeypatch.setattr(
        "leanfaith.sft1.sprint.views._mixed_200_gate_receipt",
        lambda *args, **kwargs: {"provided": True, "passed": True},
    )
    monkeypatch.setattr(
        "leanfaith.sft1.sprint.integrity._wave3_gate_issues",
        lambda **kwargs: [],
    )
    arguments: list[str] = [
        "wave3-release",
        "--repo-root",
        str(ROOT),
        "--output-dir",
        str(output),
        "--gold-blocklist",
        str(GOLD),
    ]
    for run in runs:
        arguments.extend(("--run-dir", str(run)))
    assert main(arguments) == 0
    report = json.loads((output / "release_report.json").read_text("utf-8"))
    assert report["passed"] is True
    assert report["resume_replay"]["all_zero_call"] is True
    assert report["resume_replay"]["forced_resume_observed"] is True
    assert set(report["yields"]["by_source"]) == {"mathlib", "physlib", "cslib"}
    upload_files = {path.relative_to(output).as_posix() for path in local_files(output)}
    assert "source_cache/snapshots.jsonl" in upload_files
    assert "shard-0001/rows.jsonl" in upload_files
    assert "wave3_gate_report.json" in upload_files


def test_run_directories_are_a_required_three_project_boundary(tmp_path: Path) -> None:
    runs = _runs(tmp_path)
    with pytest.raises(ViewError, match="requires explicit Mathlib, Physlib, and CSLib"):
        build_wave3_release(
            repo_root=ROOT,
            run_dirs=runs[:2],
            output_dir=tmp_path / "release",
            gold_blocklist_path=GOLD,
        )


def test_source_runs_can_be_supplied_as_any_sequence(tmp_path: Path) -> None:
    runs: Sequence[Path] = tuple(_runs(tmp_path))
    report = build_wave3_release(
        repo_root=ROOT,
        run_dirs=runs,
        output_dir=tmp_path / "release",
        gold_blocklist_path=GOLD,
        maximum_rows=12,
    )
    assert report["rows"] == 12
    assert report["n25_share"] <= 0.25


def test_multiple_completed_runs_per_project_are_fully_validated(tmp_path: Path) -> None:
    runs = _runs(tmp_path)
    second_mathlib = tmp_path / "runs" / "mathlib-second"
    _write_run(second_mathlib, "mathlib", 2, run_tag="-second")
    runs.append(second_mathlib)
    output = tmp_path / "release"

    report = build_wave3_release(
        repo_root=ROOT,
        run_dirs=runs,
        output_dir=output,
        gold_blocklist_path=GOLD,
    )

    manifest = read_json_object(output / "manifest.json")
    integrity = read_json_object(output / "integrity_report.json")
    assert len(manifest["source_runs"]) == 4
    assert len({receipt["source_key"] for receipt in manifest["source_runs"]}) == 4
    assert integrity["source_retained_files_checked"] == 4
    assert integrity["passed"] is True
    assert report["checks"]["released_rows_cover_all_three_projects"] is True


def test_wave3_integrity_rejects_tampered_cache_snapshot(tmp_path: Path) -> None:
    output = tmp_path / "release"
    build_wave3_release(
        repo_root=ROOT,
        run_dirs=_runs(tmp_path),
        output_dir=output,
        gold_blocklist_path=GOLD,
        maximum_rows=12,
    )
    manifest = read_json_object(output / "manifest.json")
    snapshot = output / manifest["source_cache_snapshot"]["file"]
    documents = [json.loads(line) for line in snapshot.read_text("utf-8").splitlines()]
    operation = next(document for document in documents if document["kind"] == "operation")
    operation["record"]["candidate_goal"] = "tampered"
    snapshot.write_bytes(b"".join(canonical_json_bytes(document) + b"\n" for document in documents))

    report = _revalidate(output)
    assert report["passed"] is False
    assert report["issue_counts"]["source_cache_snapshot"] >= 1
    assert report["issue_counts"]["source_cache_binding"] >= 1


def test_wave3_integrity_rejects_missing_cache_snapshot(tmp_path: Path) -> None:
    output = tmp_path / "release"
    build_wave3_release(
        repo_root=ROOT,
        run_dirs=_runs(tmp_path),
        output_dir=output,
        gold_blocklist_path=GOLD,
        maximum_rows=12,
    )
    manifest = read_json_object(output / "manifest.json")
    (output / manifest["source_cache_snapshot"]["file"]).unlink()

    report = _revalidate(output)
    assert report["passed"] is False
    assert report["issue_counts"]["source_cache_snapshot"] >= 1
    assert report["issue_counts"]["source_cache_binding"] >= 12


def test_wave3_integrity_rejects_manifest_snapshot_mismatch(tmp_path: Path) -> None:
    output = tmp_path / "release"
    build_wave3_release(
        repo_root=ROOT,
        run_dirs=_runs(tmp_path),
        output_dir=output,
        gold_blocklist_path=GOLD,
        maximum_rows=12,
    )
    manifest = read_json_object(output / "manifest.json")
    manifest["source_cache_snapshot"]["record_count"] += 1
    _write_json(output / "manifest.json", manifest)

    report = _revalidate(output)
    assert report["passed"] is False
    assert report["issue_counts"]["source_cache_snapshot"] >= 1


def test_wave3_integrity_rejects_mutated_source_retained_file(tmp_path: Path) -> None:
    output = tmp_path / "release"
    build_wave3_release(
        repo_root=ROOT,
        run_dirs=_runs(tmp_path),
        output_dir=output,
        gold_blocklist_path=GOLD,
        maximum_rows=12,
    )
    manifest = read_json_object(output / "manifest.json")
    retained = Path(manifest["source_retained_files"][0]["path"])
    retained.write_bytes(retained.read_bytes() + b"\n")

    report = _revalidate(output)
    assert report["passed"] is False
    assert report["issue_counts"]["source_retained_hash"] == 1
    assert report["issue_counts"]["source_run_receipt"] >= 1


@pytest.mark.parametrize("defect", ["dirty", "nonancestor"])
def test_wave3_release_rejects_unclean_or_unreachable_generator(
    tmp_path: Path, defect: str
) -> None:
    runs = _runs(tmp_path)
    manifest_path = runs[0] / "run.json"
    manifest = read_json_object(manifest_path)
    if defect == "dirty":
        manifest["implementation_dirty"] = True
    else:
        manifest["implementation_commit"] = "f" * 40
    _write_json(manifest_path, manifest)

    with pytest.raises(ViewError, match=r"dirty worktree|not an ancestor"):
        build_wave3_release(
            repo_root=ROOT,
            run_dirs=runs,
            output_dir=tmp_path / "release",
            gold_blocklist_path=GOLD,
        )


def test_wave3_release_rejects_dirty_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _runs(tmp_path)
    monkeypatch.setattr(
        "leanfaith.sft1.sprint.views._git_identity",
        lambda _root: {"commit": IMPLEMENTATION_COMMIT, "dirty": True},
    )
    with pytest.raises(ViewError, match="clean release-builder"):
        build_wave3_release(
            repo_root=ROOT,
            run_dirs=runs,
            output_dir=tmp_path / "release",
            gold_blocklist_path=GOLD,
        )


def _fixture_report(tmp_path: Path, *, operation: str, index: int) -> Path:
    run_dir = tmp_path / "fixture-runs" / f"family-{index}"
    _write_run(
        run_dir,
        "mathlib",
        2,
        run_tag=f"-fixture-{index}",
        operations=(operation,),
    )
    records = [
        json.loads(line) for line in (run_dir / "retained.jsonl").read_text("utf-8").splitlines()
    ]
    journal = [
        json.loads(line) for line in (run_dir / "journal.jsonl").read_text("utf-8").splitlines()
    ]
    rejected_root = str(records[1]["root_name"])
    journal[1] = {
        "kind": "terminal",
        "root": rejected_root,
        "operation_id": operation,
        "status": "rejected",
        "reason": "not_applicable:synthetic_typed_fixture",
    }
    (run_dir / "journal.jsonl").write_bytes(
        b"".join(canonical_json_bytes(item) + b"\n" for item in journal)
    )
    (run_dir / "retained.jsonl").write_bytes(canonical_json_bytes(records[0]) + b"\n")
    status = read_json_object(run_dir / "status.json")
    status["retained_total"] = 1
    _write_json(run_dir / "status.json", status)
    report = run_dir / "fixtures_report.json"
    _write_json(
        report,
        {
            "schema_version": 1,
            "run_id": read_json_object(run_dir / "run.json")["run_id"],
            "results": [
                {
                    "root": records[0]["root_name"],
                    "operation_id": operation,
                    "expect_status": "retained",
                    "expect_reason_prefix": "",
                    "observed_status": "retained",
                    "observed_reason": "",
                    "passed": True,
                },
                {
                    "root": rejected_root,
                    "operation_id": operation,
                    "expect_status": "rejected",
                    "expect_reason_prefix": "not_applicable",
                    "observed_status": "rejected",
                    "observed_reason": "not_applicable:synthetic_typed_fixture",
                    "passed": True,
                },
            ],
            "success_covered": [operation],
            "rejection_covered": [operation],
            "passed": True,
        },
    )
    return report


def _family_gate_receipts(tmp_path: Path) -> tuple[list[Path], frozenset[str]]:
    verdicts: list[Path] = []
    pair_ids: set[str] = set()
    for index, operation in enumerate(WAVE3_GATE_OPERATIONS):
        run_dir = tmp_path / "family-runs" / f"family-{index}"
        _write_run(
            run_dir,
            "mathlib",
            20,
            resumed=index == 0,
            run_tag=f"-family-{index}",
            operations=(operation,),
        )
        _, records = _load_completed_run(run_dir, gold_sha256=hash_file(GOLD))
        pair_ids.update(str(record["sidecar"]["pair_id"]) for record in records)
        candidate_run = tmp_path / "candidate-runs" / f"family-{index}"
        _write_run(
            candidate_run,
            "mathlib",
            100,
            resumed=True,
            run_tag=f"-candidate-{index}",
            operations=(operation,),
        )
        fixture_report = _fixture_report(tmp_path, operation=operation, index=index)
        gate_report = write_wave3_family_gate_receipt(
            operation_id=operation,
            inspection_run_dir=run_dir,
            candidate_run_dir=candidate_run,
            fixture_report_path=fixture_report,
            output_dir=tmp_path / "family-gates" / f"family-{index}",
            gold_blocklist_path=GOLD,
            rows_read_by_hand=len(records),
            wrong_labels_found=0,
        )
        verdicts.append(Path(str(gate_report["verdict_path"])))
    return verdicts, frozenset(pair_ids)


def _single_family_gate_inputs(
    tmp_path: Path, *, candidate_operations: Sequence[str]
) -> tuple[str, Path, Path, Path, list[dict[str, Any]]]:
    operation = WAVE3_GATE_OPERATIONS[0]
    inspection_run = tmp_path / "single-family" / "inspection"
    candidate_run = tmp_path / "single-family" / "candidate"
    _write_run(
        inspection_run,
        "mathlib",
        20,
        run_tag="-single-inspection",
        operations=(operation,),
    )
    _write_run(
        candidate_run,
        "mathlib",
        100,
        resumed=True,
        run_tag="-single-candidate",
        operations=candidate_operations,
    )
    _, records = _load_completed_run(inspection_run, gold_sha256=hash_file(GOLD))
    fixture = _fixture_report(tmp_path / "single-family", operation=operation, index=0)
    return operation, inspection_run, candidate_run, fixture, records


def test_wave3_family_gate_generator_binds_real_candidate_and_fixture_artifacts(
    tmp_path: Path,
) -> None:
    operation, inspection, candidate, fixture, records = _single_family_gate_inputs(
        tmp_path, candidate_operations=(WAVE3_GATE_OPERATIONS[0],)
    )
    output = tmp_path / "single-family" / "gate"
    assert (
        main(
            [
                "wave3-family-gate",
                "--operation",
                operation,
                "--inspection-run-dir",
                str(inspection),
                "--candidate-run-dir",
                str(candidate),
                "--fixture-report",
                str(fixture),
                "--output-dir",
                str(output),
                "--gold-blocklist",
                str(GOLD),
                "--rows-read-by-hand",
                str(len(records)),
                "--wrong-labels-found",
                "0",
            ]
        )
        == 0
    )
    report = read_json_object(output / "gate_build_report.json")
    verdict = Path(str(report["verdict_path"]))
    pair_ids = frozenset(str(record["sidecar"]["pair_id"]) for record in records)
    validation = _inspection_receipts(
        [verdict], released_pair_ids=pair_ids, gold_sha256=hash_file(GOLD)
    )
    receipt = validation["receipts"][0]
    assert receipt["passed"] is True
    assert receipt["candidate_run_receipt"]["performance"]["roots_considered"] == 100
    assert receipt["candidate_audit"]["run_receipt"] == receipt["candidate_run_receipt"]
    assert receipt["candidate_audit"]["terminal_accounting"]["terminal_count"] == 100
    assert receipt["fixture_receipt"]["passed"] is True

    audit_path = Path(str(read_json_object(verdict)["candidate_audit_path"]))
    audit = read_json_object(audit_path)
    audit["failure_taxonomy"] = {"fabricated": 100}
    _write_json(audit_path, audit)
    verdict_document = read_json_object(verdict)
    verdict_document["candidate_audit_sha256"] = hash_file(audit_path)
    _write_json(verdict, verdict_document)
    tampered = _inspection_receipts(
        [verdict], released_pair_ids=pair_ids, gold_sha256=hash_file(GOLD)
    )
    assert tampered["receipts"][0]["passed"] is False


def test_wave3_family_gate_generator_rejects_mixed_operation_candidate_run(
    tmp_path: Path,
) -> None:
    operation, inspection, candidate, fixture, records = _single_family_gate_inputs(
        tmp_path,
        candidate_operations=(WAVE3_GATE_OPERATIONS[0], "P18_SYMMETRIZE_EQUALITY_V1"),
    )
    output = tmp_path / "single-family" / "gate"
    with pytest.raises(ViewError, match="exact_operation"):
        write_wave3_family_gate_receipt(
            operation_id=operation,
            inspection_run_dir=inspection,
            candidate_run_dir=candidate,
            fixture_report_path=fixture,
            output_dir=output,
            gold_blocklist_path=GOLD,
            rows_read_by_hand=len(records),
            wrong_labels_found=0,
        )
    assert not output.exists()


def _mixed_gate_report(tmp_path: Path) -> Path:
    definitions = (
        ("mathlib", (WAVE3_GATE_OPERATIONS[0], WAVE3_GATE_OPERATIONS[3]), True),
        ("physlib", (WAVE3_GATE_OPERATIONS[2], WAVE3_GATE_OPERATIONS[4]), False),
    )
    sources: list[dict[str, Any]] = []
    useful: set[str] = set()
    for index, (project, operations, resumed) in enumerate(definitions):
        run_dir = tmp_path / "mixed-runs" / project
        _write_run(
            run_dir,
            project,
            100,
            resumed=resumed,
            run_tag=f"-mixed-{index}",
            operations=operations,
        )
        receipt, records = _load_completed_run(run_dir, gold_sha256=hash_file(GOLD))
        useful.update(str(record["sidecar"]["operation_id"]) for record in records)
        sources.append(
            {
                "run_dir": str(run_dir),
                "run_id": receipt["run_id"],
                "run_receipt_sha256": hash_canonical(receipt),
            }
        )
    report = tmp_path / "mixed-200-gate.json"
    checks = {
        "zero_wrong_labels": True,
        "exact_negative_separators": True,
        "exact_positive_equivalences": True,
        "zero_self_pairs": True,
        "zero_partial_groups": True,
        "zero_conflicts": True,
        "zero_duplicate_stable_ids": True,
        "forced_resume": True,
        "zero_call_replay": True,
    }
    _write_json(
        report,
        {
            "schema_version": 1,
            "kind": "sft1_wave3_mixed_200_gate_v1",
            "roots_considered": 200,
            "source_runs": sources,
            "useful_negative_families": sorted(useful),
            "wrong_labels_found": 0,
            "checks": checks,
            "passed": True,
        },
    )
    return report


def test_wave3_strict_family_and_mixed_gates_pass_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verdicts, inspected_pair_ids = _family_gate_receipts(tmp_path)
    inspections = _inspection_receipts(
        verdicts,
        released_pair_ids=inspected_pair_ids,
        gold_sha256=hash_file(GOLD),
    )
    mixed_path = _mixed_gate_report(tmp_path)
    mixed = _mixed_200_gate_receipt(mixed_path, gold_sha256=hash_file(GOLD))
    assert inspections["passed"] is True
    assert inspections["exact_family_set"] is True
    assert inspections["useful_family_count"] == 5
    assert all(receipt["sample_exact_run"] for receipt in inspections["receipts"])
    assert mixed["passed"] is True
    assert mixed["roots_considered"] == 200
    assert set(mixed["projects"]) == {"mathlib", "physlib"}

    passing_screens = {
        "screens": [
            {"name": "candidate_only", "passed": True},
            {"name": "reference_only", "passed": True},
            {"name": "family_held_out", "passed": True},
        ],
        "passed": True,
    }
    monkeypatch.setattr(
        "leanfaith.sft1.sprint.views._screen_results", lambda _records: passing_screens
    )
    output = tmp_path / "strict-release"
    release_runs = _runs(tmp_path / "release-source")
    release_runs.extend(Path(str(read_json_object(verdict)["run_dir"])) for verdict in verdicts)
    changed_companions = tmp_path / "release-source" / "runs" / "changed-companions"
    same_companions = tmp_path / "release-source" / "runs" / "same-companions"
    _write_run(
        changed_companions,
        "mathlib",
        80,
        run_tag="-changed-companions",
        operations=("P_ORDER_COMPLEMENT_V1",),
    )
    _write_run(
        same_companions,
        "mathlib",
        20,
        run_tag="-same-companions",
        operations=("P18_SYMMETRIZE_EQUALITY_V1",),
    )
    release_runs.extend((changed_companions, same_companions))
    report = build_wave3_release(
        repo_root=ROOT,
        run_dirs=release_runs,
        output_dir=output,
        gold_blocklist_path=GOLD,
        inspection_verdict_paths=verdicts,
        mixed_200_gate_report=mixed_path,
    )
    assert report["passed"] is True
    assert report["wave3_gate"]["passed"] is True
    assert all(
        receipt["sample_selection_bound"]
        for receipt in report["wave3_gate"]["family_gates"]["receipts"]
    )
    assert read_json_object(output / "integrity_report.json")["passed"] is True
    upload_set = {path.relative_to(output).as_posix() for path in local_files(output)}
    assert "wave3_gate_report.json" in upload_set


@pytest.mark.parametrize("defect", ["one_row", "bad_receipt", "missing_family"])
def test_wave3_family_gate_rejects_incomplete_inspection_evidence(
    tmp_path: Path, defect: str
) -> None:
    verdicts, pair_ids = _family_gate_receipts(tmp_path)
    if defect == "one_row":
        verdict = read_json_object(verdicts[0])
        sample = Path(verdict["sample_path"])
        first = sample.read_text("utf-8").splitlines()[0]
        sample.write_text(first + "\n", encoding="utf-8")
        verdict["rows_read_by_hand"] = 1
        verdict["sample_sha256"] = hash_file(sample)
        _write_json(verdicts[0], verdict)
    elif defect == "bad_receipt":
        verdict = read_json_object(verdicts[0])
        verdict["run_receipt_sha256"] = "0" * 64
        _write_json(verdicts[0], verdict)
    else:
        verdicts.pop()
    result = _inspection_receipts(
        verdicts,
        released_pair_ids=pair_ids,
        gold_sha256=hash_file(GOLD),
    )
    assert result["passed"] is False

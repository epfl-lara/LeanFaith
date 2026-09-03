"""Lean-free integrity tests for Wave 3 target and mixed-gate receipts."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.loading import LoadedConfig
from leanfaith.config.paths import find_repo_root
from leanfaith.lean.protocol import LeanStatus
from leanfaith.sft1.sprint import engine
from leanfaith.sft1.sprint import runner as runner_module
from leanfaith.sft1.sprint.inventory import Declaration, write_inventory
from leanfaith.sft1.sprint.runner import (
    SprintConfig,
    SprintRunner,
    SprintRunnerError,
    load_sprint_config,
    parse_family_quotas,
    read_roots_file,
    target_family_matches,
    wave3_mixed_gate_report,
    write_mixed_family_targets,
)

ROOT = find_repo_root(Path(__file__))
WAVE3_CONFIG = ROOT / "configs/transformations/sft1_value_first_v1/wave3_v1.yaml"
CHECKED = {"meta_checked": True, "kernel_checked": True}
OPERATIONS = (
    "P18_SYMMETRIZE_EQUALITY_V1",
    "N25_TOGGLE_EQ_NE_PROOF_V1",
    "N31_DROP_REQUIRED_GUARD_PROOF_V1",
    "N30_ADD_UNJUSTIFIED_UNIQUENESS_PROOF_V1",
    "N29_SWAP_WITNESS_DEPENDENCY_PROOF_V1",
)


@pytest.fixture(autouse=True)
def _stub_expensive_shortcut_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    def screen(records: Sequence[Mapping[str, Any]]) -> dict[str, object]:
        results = [
            {"name": name, "passed": True, "threshold": threshold}
            for name, threshold in (
                ("candidate_only", 0.60),
                ("reference_only", 0.60),
                ("family_held_out", 0.65),
            )
        ]
        return {"rows": len(records), "screens": results, "passed": True}

    monkeypatch.setattr(runner_module, "run_screens_v3", screen)


def _loaded_with_inventory(
    tmp_path: Path, declarations: Sequence[Declaration] | None = None
) -> LoadedConfig[SprintConfig]:
    loaded = load_sprint_config(ROOT, WAVE3_CONFIG)
    revision = loaded.config.project.project_revision
    inventory_root = tmp_path / "inventory"
    write_inventory(
        declarations
        or [
            Declaration("Example.a", "Example", "Example.lean", 1, "theorem", "theorem a : True"),
            Declaration("Example.b", "Example", "Example.lean", 2, "theorem", "theorem b : True"),
        ],
        inventory_root / revision,
        project_id="mathlib",
        project_revision=revision,
    )
    inventory = loaded.config.inventory.model_copy(update={"root": str(inventory_root)})
    output = loaded.config.output.model_copy(update={"staging_root": str(tmp_path / "stage")})
    config = loaded.config.model_copy(update={"inventory": inventory, "output": output})
    return LoadedConfig(
        config=config,
        path=loaded.path,
        raw=loaded.raw,
        config_hash=hash_canonical(config.model_dump(mode="json")),
    )


def _loaded_with_mixed_candidates(tmp_path: Path) -> LoadedConfig[SprintConfig]:
    statements = (
        ("Example.boundA", "theorem boundA (n i : Nat) : i < n → True"),
        ("Example.boundB", "theorem boundB (n i : Nat) : i ∈ Finset.range n → True"),
        ("Example.nonzero", "theorem nonzero (n : Nat) : n ≠ 0 → True"),
        ("Example.positive", "theorem positive (n : Nat) : 0 < n → True"),
        ("Example.order", "theorem order (a b : Nat) : a < b"),
        ("Example.existsBool", "theorem existsBool : ∃ x : Bool, x = x"),
        (
            "Example.forallExistsBool",
            "theorem forallExistsBool : ∀ x : Bool, ∃ y : Bool, x = y",
        ),
        ("Example.eq", "theorem eq (a b : Nat) : a = b"),
    )
    declarations = [
        Declaration(name, "Example", "Example.lean", index, "theorem", statement)
        for index, (name, statement) in enumerate(statements, start=1)
    ]
    return _loaded_with_inventory(tmp_path, declarations)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def test_roots_file_normalizes_mapping_entries_and_binds_complete_identity(
    tmp_path: Path,
) -> None:
    loaded = _loaded_with_inventory(tmp_path)
    inventory_path = (
        Path(loaded.config.inventory.root)
        / loaded.config.project.project_revision
        / "inventory.jsonl"
    )
    roots = ["Example.a", "Example.b"]
    payload = {
        "schema_version": 1,
        "target_kind": "test_candidates",
        "project_id": "mathlib",
        "project_revision": loaded.config.project.project_revision,
        "inventory_sha256": hash_file(inventory_path),
        "count": 2,
        "roots_sha256": hash_canonical(roots),
        "roots": [roots[0], {"name": roots[1], "statement": "theorem b : True"}],
    }
    path = tmp_path / "roots.json"
    _write_json(path, payload)

    selection = read_roots_file(path, loaded, expected_file_sha256=hash_file(path))

    assert selection.roots == tuple(roots)
    assert selection.identity == {
        "schema_version": 1,
        "file_sha256": hash_file(path),
        "metadata_sha256": hash_canonical({k: v for k, v in payload.items() if k != "roots"}),
        "project_id": "mathlib",
        "project_revision": loaded.config.project.project_revision,
        "inventory_sha256": hash_file(inventory_path),
        "count": 2,
        "roots_sha256": hash_canonical(roots),
    }


def test_roots_file_accepts_legacy_roots_only_shape(tmp_path: Path) -> None:
    loaded = _loaded_with_inventory(tmp_path)
    path = tmp_path / "roots.json"
    _write_json(path, {"roots": ["Example.a"]})

    selection = read_roots_file(path, loaded)

    assert selection.roots == ("Example.a",)
    assert selection.identity["count"] == 1
    assert selection.identity["roots_sha256"] == hash_canonical(["Example.a"])
    assert selection.identity["file_sha256"] == hash_file(path)


@pytest.mark.parametrize(
    ("field", "bad_value", "message"),
    [
        ("project_id", "physlib", "project_id mismatch"),
        ("project_revision", "wrong", "project_revision mismatch"),
        ("inventory_sha256", "0" * 64, "inventory_sha256 mismatch"),
        ("count", 3, "count mismatch"),
        ("roots_sha256", "0" * 64, "roots_sha256 mismatch"),
    ],
)
def test_roots_file_rejects_conflicting_declared_metadata(
    tmp_path: Path, field: str, bad_value: object, message: str
) -> None:
    loaded = _loaded_with_inventory(tmp_path)
    path = tmp_path / "roots.json"
    _write_json(path, {field: bad_value, "roots": ["Example.a"]})
    with pytest.raises(SprintRunnerError, match=message):
        read_roots_file(path, loaded)


@pytest.mark.parametrize(
    "roots",
    [
        [{"root_name": "Example.a"}],
        [{"name": "Example.a", "root": "Example.b"}],
        ["Example.a", {"name": "Example.a"}],
        ["Example.missing"],
    ],
)
def test_roots_file_rejects_ambiguous_duplicate_or_unknown_entries(
    tmp_path: Path, roots: list[object]
) -> None:
    loaded = _loaded_with_inventory(tmp_path)
    path = tmp_path / "roots.json"
    _write_json(path, {"roots": roots})
    with pytest.raises(SprintRunnerError):
        read_roots_file(path, loaded)


def test_roots_file_sha_and_identity_are_resume_bound(tmp_path: Path) -> None:
    loaded = load_sprint_config(ROOT, WAVE3_CONFIG)
    output = loaded.config.output.model_copy(update={"staging_root": str(tmp_path / "stage")})
    config = loaded.config.model_copy(update={"output": output})
    loaded = LoadedConfig(
        config=config,
        path=loaded.path,
        raw=loaded.raw,
        config_hash=hash_canonical(config.model_dump(mode="json")),
    )
    first_path = tmp_path / "first.json"
    _write_json(first_path, {"roots": ["PNat.gcd_comm"]})
    selection = read_roots_file(first_path, loaded)
    runner = SprintRunner(
        ROOT,
        loaded,
        run_id="identity",
        explicit_roots=selection.roots,
        operations=["P18_SYMMETRIZE_EQUALITY_V1"],
        roots_file=selection,
    )
    runner.write_run_manifest(order_size=1)
    manifest = json.loads(runner.paths.run_manifest.read_text(encoding="utf-8"))
    assert manifest["roots_file_identity"] == selection.identity
    assert manifest["roots_file_path"] == str(first_path.resolve())

    second_path = tmp_path / "second.json"
    _write_json(
        second_path, {"note": "same roots, different source bytes", "roots": ["PNat.gcd_comm"]}
    )
    changed = read_roots_file(second_path, loaded)
    resumed = SprintRunner(
        ROOT,
        loaded,
        run_id="identity",
        explicit_roots=changed.roots,
        operations=["P18_SYMMETRIZE_EQUALITY_V1"],
        roots_file=changed,
    )
    with pytest.raises(SprintRunnerError, match="roots_file_identity changed"):
        resumed.write_run_manifest(order_size=1)
    with pytest.raises(SprintRunnerError, match="SHA-256 mismatch"):
        read_roots_file(second_path, loaded, expected_file_sha256="0" * 64)


def test_mixed_target_builder_is_deterministic_across_quota_argument_order(
    tmp_path: Path,
) -> None:
    loaded = _loaded_with_mixed_candidates(tmp_path)
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    first_report = write_mixed_family_targets(
        loaded,
        family_quotas=parse_family_quotas(["N31=2", "N26=1"]),
        selection_salt="mixed-target-test-v1",
        out=first,
    )
    second_report = write_mixed_family_targets(
        loaded,
        family_quotas=parse_family_quotas(
            ["N26_INCREMENT_BOUND_PROOF_V1=1", "N31_DROP_REQUIRED_GUARD_PROOF_V1=2"]
        ),
        selection_salt="mixed-target-test-v1",
        out=second,
    )

    assert first.read_bytes() == second.read_bytes()
    assert first_report == second_report
    assert first_report["family_order"] == ["N26", "N31"]
    assert first_report["family_quotas"] == [
        {"target_family": "N26", "quota": 1},
        {"target_family": "N31", "quota": 2},
    ]


def test_mixed_target_builder_deduplicates_overlap_and_round_trips_strictly(
    tmp_path: Path,
) -> None:
    loaded = _loaded_with_mixed_candidates(tmp_path)
    out = tmp_path / "mixed.json"

    report = cast(
        dict[str, Any],
        write_mixed_family_targets(
            loaded,
            family_quotas=(("N26", 1), ("N31", 2), ("N32", 1)),
            selection_salt="mixed-target-overlap-v1",
            out=out,
        ),
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    assignments = payload["roots"]
    names = [entry["name"] for entry in assignments]
    selection = read_roots_file(out, loaded, expected_file_sha256=hash_file(out))

    assert len(names) == len(set(names)) == 4
    assert selection.roots == tuple(names)
    assert [entry["target_family"] for entry in assignments].count("N26") == 1
    assert [entry["target_family"] for entry in assignments].count("N31") == 2
    assert [entry["target_family"] for entry in assignments].count("N32") == 1
    assert report["eligible_counts_before_dedup"]["N31"] == 4
    assert report["available_counts_after_prior_family_dedup"]["N31"] == 3
    assert report["project_id"] == loaded.config.project.project_id
    assert report["project_revision"] == loaded.config.project.project_revision
    inventory_path = (
        Path(loaded.config.inventory.root)
        / loaded.config.project.project_revision
        / "inventory.jsonl"
    )
    assert report["inventory_sha256"] == hash_file(inventory_path)
    assert report["selection_salt"] == "mixed-target-overlap-v1"
    assert report["count"] == 4
    assert report["roots_sha256"] == hash_canonical(names)
    assert report["assignments_sha256"] == hash_canonical(assignments)
    assert selection.identity["file_sha256"] == hash_file(out)

    tampered = tmp_path / "tampered.json"
    _write_json(tampered, {**payload, "assignments_sha256": "0" * 64})
    with pytest.raises(SprintRunnerError, match="assignment hash mismatch"):
        read_roots_file(tampered, loaded)


def test_mixed_target_builder_fails_before_write_when_dedup_quota_is_short(
    tmp_path: Path,
) -> None:
    loaded = _loaded_with_mixed_candidates(tmp_path)
    out = tmp_path / "short.json"

    with pytest.raises(SprintRunnerError, match="cannot be met after deterministic"):
        write_mixed_family_targets(
            loaded,
            family_quotas=(("N26", 2), ("N31", 3)),
            selection_salt="mixed-target-shortage-v1",
            out=out,
        )

    assert not out.exists()


@pytest.mark.parametrize(
    "values",
    (["N26=0"], ["N26=01"], ["N26=x"], ["N26=1", "N26_INCREMENT_BOUND_PROOF_V1=2"]),
)
def test_mixed_target_quota_parser_rejects_noncanonical_or_duplicate_values(
    values: list[str],
) -> None:
    with pytest.raises(SprintRunnerError):
        parse_family_quotas(values)


def test_n26_target_hint_accepts_only_direct_or_explicit_bounded_binders() -> None:
    assert target_family_matches("theorem direct_lt (n i : Nat) : i < n → P i", "N26")
    assert target_family_matches("theorem direct_mem (n i : Nat) : i ∈ Finset.range n → P i", "N26")
    assert target_family_matches("theorem explicit (n : Nat) : ∀ i < n, P i", "N26")
    assert not target_family_matches("theorem nested (n i : Nat) : Q i → i < n → P i", "N26")
    assert not target_family_matches("theorem not_bound (i : Nat) : i < n → P i", "N26")
    assert not target_family_matches("theorem identical (i : Nat) : i < i → P i", "N26")


class _FakeProcessSession:
    def __init__(self, result: engine.ProcessResult) -> None:
        self.result = result
        self.calls: list[tuple[tuple[tuple[str, int], ...], str]] = []

    def run_process(
        self, roots: Sequence[tuple[str, int]], *, request_id: str
    ) -> engine.ProcessResult:
        self.calls.append((tuple(roots), request_id))
        return self.result


class _FakeBackend:
    def __init__(self) -> None:
        self.resets = 0

    def reset_session(self) -> None:
        self.resets += 1


def test_singleton_infrastructure_failure_is_bounded_and_left_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = SprintRunner.__new__(SprintRunner)
    runner.run_id = "infrastructure-retry"
    runner.batches = 0
    runner.backend = _FakeBackend()
    session = _FakeProcessSession(
        engine.ProcessResult(
            roots={},
            request_hash="a" * 64,
            elapsed_ms=1,
            raw_response_path=None,
            status=LeanStatus.CRASH.value,
            errors=("injected crash",),
        )
    )
    failures: list[tuple[str, Mapping[str, Any], str]] = []

    def capture_failure(name: str, payload: Mapping[str, Any], *, source: str) -> None:
        failures.append((name, payload, source))

    monkeypatch.setattr(runner, "open_session", lambda: session)
    monkeypatch.setattr(runner, "finalize_root_failure", capture_failure)
    with pytest.raises(SprintRunnerError, match="after bounded retries"):
        runner.process_batch([("Example.root", 1)])

    assert len(session.calls) == 2
    assert session.calls[0][1] != session.calls[1][1]
    assert runner.backend.resets == 1
    assert failures == []


@pytest.mark.parametrize("status", [LeanStatus.INVALID.value, LeanStatus.VALID_WITH_SORRY.value])
def test_deterministic_batch_failure_is_terminal_without_recursive_bisection(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    runner = SprintRunner.__new__(SprintRunner)
    runner.run_id = "deterministic-terminal"
    runner.batches = 0
    session = _FakeProcessSession(
        engine.ProcessResult(
            roots={},
            request_hash="a" * 64,
            elapsed_ms=1,
            raw_response_path=None,
            status=status,
            errors=("deterministic compiler error",),
        )
    )
    failures: list[tuple[str, Mapping[str, Any], str]] = []

    def capture_failure(name: str, payload: Mapping[str, Any], *, source: str) -> None:
        failures.append((name, payload, source))

    monkeypatch.setattr(runner, "open_session", lambda: session)
    monkeypatch.setattr(runner, "finalize_root_failure", capture_failure)
    roots = [(f"Example.root{i}", 1) for i in range(8)]

    runner.process_batch(roots)

    assert len(session.calls) == 1
    assert runner.batches == 1
    assert [name for name, _, _ in failures] == [name for name, _ in roots]
    assert all(payload["root_status"] == "error" for _, payload, _ in failures)
    assert all(f"request_{status}" in str(payload["reason"]) for _, payload, _ in failures)
    assert {source for _, _, source in failures} == {"lean"}


def _evidence(operation: str) -> dict[str, object]:
    if operation == "P18_SYMMETRIZE_EQUALITY_V1":
        return {
            "candidate_truth": "proved_equivalent_to_reference",
            "equivalence_proof": {"check": CHECKED},
        }
    refutation: dict[str, object] = {"check": CHECKED}
    if operation == "N31_DROP_REQUIRED_GUARD_PROOF_V1":
        refutation["separator"] = {"check": CHECKED}
    elif operation == "N30_ADD_UNJUSTIFIED_UNIQUENESS_PROOF_V1":
        refutation.update(
            {
                "witnesses": ["false", "true"],
                "witness_checks": [CHECKED, CHECKED, CHECKED],
                "enumeration": "complete_finite_domain",
            }
        )
    elif operation == "N29_SWAP_WITNESS_DEPENDENCY_PROOF_V1":
        refutation.update(
            {
                "witness_checks": [CHECKED],
                "enumeration": "complete_matrix",
            }
        )
    return {
        "candidate_truth": "refuted",
        "source_proof_check": CHECKED,
        "refutation": refutation,
    }


def _record(project: str, revision: str, root: str, operation: str) -> dict[str, Any]:
    label = operation == "P18_SYMMETRIZE_EQUALITY_V1"
    token = hash_canonical([project, root, operation])
    pair_id = f"pair:{token}"
    root_id = f"root:{hash_canonical([project, root])}"
    return {
        "row": {
            "pair_id": pair_id,
            "root_id": root_id,
            "reference": f"x : Nat\n⊢ x = {token[:8]}",
            "candidate": f"x : Nat\n⊢ {token[8:16]} = x",
            "label": label,
            "operation_id": operation,
        },
        "sidecar": {
            "pair_id": pair_id,
            "root_id": root_id,
            "root_name": root,
            "operation_id": operation,
            "mechanism": operation.split("_", 1)[0],
            "label": label,
            "engine": {"semantic_version": "sft1_wave3_engine_v1"},
            "project": {"project_id": project, "project_revision": revision},
            "evidence": _evidence(operation),
        },
        "row_hash": token,
        "unordered_pair_key": hash_canonical(["unordered", token]),
        "label": label,
        "operation_id": operation,
        "root_name": root,
    }


def _write_project_run(run_dir: Path, project: str, root_count: int, *, resumed: bool) -> None:
    revision = f"{project}-revision"
    roots = [f"{project}.root{i:03d}" for i in range(root_count)]
    records = [
        _record(project, revision, root, operation) for root in roots for operation in OPERATIONS
    ]
    manifest = {
        "schema_version": 1,
        "run_id": f"mixed-{project}",
        "project": {"project_id": project, "project_revision": revision},
        "operations": list(OPERATIONS),
        "explicit_roots": roots,
        "explicit_roots_sha256": hash_canonical(roots),
        "roots_file_identity": {
            "schema_version": 1,
            "file_sha256": hash_canonical([project, "file"]),
            "metadata_sha256": hash_canonical([project, "metadata"]),
            "project_id": project,
            "project_revision": revision,
            "inventory_sha256": hash_canonical([project, "inventory"]),
            "count": len(roots),
            "roots_sha256": hash_canonical(roots),
        },
    }
    status = {
        "run_id": manifest["run_id"],
        "final": True,
        "roots_considered": len(roots),
        "roots_this_process": len(roots) - 1 if resumed else len(roots),
        "retained_total": len(records),
    }
    replay = {
        "run_id": manifest["run_id"],
        "lean_requests": 0,
        "duplicate_rows": 0,
        "retained_before": len(records),
        "retained_after": len(records),
        "roots_considered": len(roots),
    }
    journal: list[dict[str, Any]] = []
    by_cell = {
        (str(record["root_name"]), str(record["operation_id"])): record for record in records
    }
    for root in roots:
        journal.append({"kind": "root", "root": root, "source": "lean"})
        for operation in OPERATIONS:
            record = by_cell[(root, operation)]
            journal.append(
                {
                    "kind": "terminal",
                    "root": root,
                    "operation_id": operation,
                    "status": "retained",
                    "pair_id": record["row"]["pair_id"],
                    "unordered_pair_key": record["unordered_pair_key"],
                }
            )
    _write_json(run_dir / "run.json", manifest)
    _write_json(run_dir / "status.json", status)
    _write_json(run_dir / "replay_report.json", replay)
    (run_dir / "journal.jsonl").write_bytes(
        b"".join(canonical_json_bytes(row) + b"\n" for row in journal)
    )
    (run_dir / "retained.jsonl").write_bytes(
        b"".join(canonical_json_bytes(row) + b"\n" for row in records)
    )


def _mixed_runs(tmp_path: Path) -> list[Path]:
    runs = [tmp_path / project for project in ("mathlib", "physlib", "cslib")]
    for run, project, count in zip(
        runs, ("mathlib", "physlib", "cslib"), (140, 40, 20), strict=True
    ):
        _write_project_run(run, project, count, resumed=project == "mathlib")
    return runs


def test_wave3_mixed_gate_passes_complete_three_project_receipts(tmp_path: Path) -> None:
    report = wave3_mixed_gate_report(_mixed_runs(tmp_path))

    assert report["passed"] is True
    assert report["selected_roots"] == report["qualified_roots"] == 200
    assert report["projects"] == ["cslib", "mathlib", "physlib"]
    assert report["retained_rows"] == 1000
    assert report["n25_share"] == 0.2
    assert len(report["useful_negative_families"]) == 4
    assert report["pair_delta_diagnostics"]["rows"] == 1000
    assert report["shortcut_screens"]["passed"] is True
    assert report["checks"]["candidate_only_shortcut_screen_passed"] is True
    assert report["checks"]["reference_only_shortcut_screen_passed"] is True
    assert all(report["checks"].values())


def test_wave3_mixed_gate_enforces_existing_shortcut_thresholds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runner_module,
        "run_screens_v3",
        lambda records: {
            "rows": len(records),
            "screens": [
                {"name": "candidate_only", "passed": False, "threshold": 0.60},
                {"name": "reference_only", "passed": True, "threshold": 0.60},
                {"name": "family_held_out", "passed": True, "threshold": 0.65},
            ],
            "passed": False,
        },
    )

    report = wave3_mixed_gate_report(_mixed_runs(tmp_path))

    assert report["passed"] is False
    assert report["checks"]["candidate_only_shortcut_screen_passed"] is False
    assert report["checks"]["existing_shortcut_screen_contract_passed"] is False


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda run: _mutate_jsonl(run / "journal.jsonl", lambda rows: rows[:-1]),
            id="missing-terminal",
        ),
        pytest.param(
            lambda run: _mutate_jsonl(
                run / "retained.jsonl",
                lambda rows: [
                    {
                        **rows[0],
                        "row": {
                            **rows[0]["row"],
                            "candidate": rows[0]["row"]["reference"],
                        },
                    },
                    *rows[1:],
                ],
            ),
            id="self-pair",
        ),
        pytest.param(
            lambda run: _mutate_jsonl(
                run / "retained.jsonl",
                lambda rows: [
                    rows[0],
                    {**rows[1], "row": {**rows[1]["row"], "pair_id": rows[0]["row"]["pair_id"]}},
                    *rows[2:],
                ],
            ),
            id="duplicate-pair-id",
        ),
    ],
)
def test_wave3_mixed_gate_fails_closed_on_integrity_defects(
    tmp_path: Path, mutate: Callable[[Path], None]
) -> None:
    runs = _mixed_runs(tmp_path)
    mutate(runs[0])
    report = wave3_mixed_gate_report(runs)
    assert report["passed"] is False
    assert not all(report["checks"].values())


def _mutate_jsonl(
    path: Path, mutate: Callable[[list[dict[str, Any]]], list[dict[str, Any]]]
) -> None:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    changed = mutate(rows)
    path.write_bytes(b"".join(canonical_json_bytes(row) + b"\n" for row in changed))

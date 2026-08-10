"""LF-031 read-only deterministic-v2 coverage probe."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.schemas import CANONICAL_VIEW_NAMES, ViewStatus, make_id
from leanfaith.transforms.v2_contract import EXPECTED_V2_FAMILY_IDS
from leanfaith.transforms.v2_coverage import (
    V2CoverageError,
    build_v2_coverage_report,
    run_v2_coverage_probe,
)
from tests.unit.record_factories import representation_record


def _record(
    key: str,
    text: str,
    *,
    raw: str | None = None,
    semantic_atoms: tuple[str, ...] | None = None,
    operator_tree: dict[str, object] | None = None,
) -> object:
    statuses = {
        name: (
            ViewStatus.OK
            if name in {"raw_proof_stripped", "headless", "signature_pp"}
            else ViewStatus.NOT_ATTEMPTED
        )
        for name in CANONICAL_VIEW_NAMES
    }
    if semantic_atoms is not None:
        statuses["semantic_atoms"] = ViewStatus.OK
    if operator_tree is not None:
        statuses["operator_tree"] = ViewStatus.OK
    return representation_record(
        representation_id=make_id("repr", {"v2_coverage": key}),
        theorem_id=make_id("thm", {"v2_coverage": key}),
        raw_proof_stripped=raw or f"theorem {key} {text} := by sorry",
        headless=text,
        signature_pp=text,
        semantic_atoms=semantic_atoms,
        operator_tree=operator_tree,
        view_status=statuses,
        content_hash=key.encode("utf-8").hex().ljust(64, "0")[:64],
    )


def _write_jsonl(path: Path, records: list[object]) -> None:
    with path.open("wb") as handle:
        for record in records:
            payload = record.model_dump(mode="json")  # type: ignore[attr-defined]
            handle.write(canonical_json_bytes(payload) + b"\n")


def _coverage_input(tmp_path: Path) -> Path:
    path = tmp_path / "representations.jsonl"
    _write_jsonl(
        path,
        [
            _record(
                "a",
                ": ∀ x ∈ s, P x → Q x",
                raw="theorem a : ∀ x ∈ s, P x → Q x := by sorry",
                semantic_atoms=("const:Coe.coe", "const:Set.mem"),
                operator_tree={"k": "app", "children": [{"k": "proj"}]},
            ),
            _record(
                "b",
                ": ∀ (x y : Nat), ∃ z, R x y z ∧ S z ∧ T z",
                raw=("theorem b : ∀ (x y : Nat), ∃ z, R x y z ∧ S z ∧ T z := by sorry"),
            ),
        ],
    )
    return path


def test_coverage_probe_is_deterministic_read_only_and_design_only(tmp_path: Path) -> None:
    source = _coverage_input(tmp_path)
    before = source.read_bytes()
    before_hash = hash_file(source)

    first = build_v2_coverage_report(representations_path=source)
    second = build_v2_coverage_report(representations_path=source)

    assert first.report_id == second.report_id
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert source.read_bytes() == before
    assert hash_file(source) == before_hash == first.representations_sha256
    assert first.representation_record_count == 2
    assert tuple(item.family_id for item in first.family_coverage) == EXPECTED_V2_FAMILY_IDS
    assert first.lean_requests_executed == 0
    assert first.drafts_emitted == 0
    assert first.labels_emitted == 0
    assert first.inputs_mutated is False

    by_family = {item.family_id: item for item in first.family_coverage}
    assert by_family["p07_coercion_surface"].theorem_hit_count == 1
    assert by_family["p09_projections"].theorem_hit_count == 1
    assert by_family["p11_bounded_quantifiers"].theorem_hit_count == 1
    assert by_family["p12_proof_arrow_binder"].theorem_hit_count == 1
    assert by_family["n13_witness_dependency"].theorem_hit_count == 1
    assert by_family["n15_conjunct_omission"].theorem_hit_count == 1


def test_coverage_report_write_is_create_only(tmp_path: Path) -> None:
    source = _coverage_input(tmp_path)
    output = tmp_path / "coverage.json"
    report, digest = run_v2_coverage_probe(
        representations_path=source,
        output_path=output,
    )
    assert hash_file(output) == digest
    assert json.loads(output.read_text(encoding="utf-8"))["report_id"] == report.report_id
    with pytest.raises(V2CoverageError, match="already exists"):
        run_v2_coverage_probe(representations_path=source, output_path=output)


def test_coverage_probe_rejects_duplicate_theorem_ids(tmp_path: Path) -> None:
    source = tmp_path / "duplicate.jsonl"
    record = _record("duplicate", ": True")
    _write_jsonl(source, [record, record])
    with pytest.raises(V2CoverageError, match="duplicate theorem_id"):
        build_v2_coverage_report(representations_path=source)


def test_cli_reports_hard_zero_emission_counts(tmp_path: Path) -> None:
    source = _coverage_input(tmp_path)
    output = tmp_path / "cli-coverage.json"
    result = CliRunner().invoke(
        app,
        [
            "probe-deterministic-v2-coverage",
            "--representations",
            str(source),
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "lean_requests=0 drafts=0 labels=0" in result.output
    assert output.is_file()

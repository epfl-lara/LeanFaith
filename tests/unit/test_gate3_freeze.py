"""Gate-3 input freeze and bounded representation partition tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from leanfaith.cli.pipeline import (
    _cross_path_exclusion_reason,
    _inline_alias_of,
    _merge_count_maps,
    _merge_representation_partitions,
    _read_chunk_marker,
    _select_cross_path_inputs,
    _validate_frozen_gate3_manifest,
    _write_chunk_marker,
    run_freeze_gate3_inputs,
)
from leanfaith.config.hashing import hash_file
from leanfaith.schemas import ViewStatus, make_id
from tests.unit.record_factories import representation_record, theorem_record


def _write_theorems(
    path: Path,
    source: str,
    count: int,
    *,
    context_id: str = "ctx:" + "0" * 64,
) -> None:
    rows = []
    for index in range(count):
        theorem_id = make_id("thm", {"source": source, "index": index})
        ancestry_id = make_id("anc", {"source": source, "index": index})
        record = theorem_record(
            theorem_id=theorem_id,
            ancestry_id=ancestry_id,
            root_ancestry_ids=(ancestry_id,),
            source=source,
            context_id=context_id,
            declaration_name=f"t_{source}_{index}",
            declaration_full_name=f"t_{source}_{index}",
            statement_content_hash=f"{index + 3:064x}",
            metadata={"transform_source_eligible": True},
        )
        rows.append(
            json.dumps(
                {
                    "theorem": record.model_dump(mode="json"),
                    "representation": {"headless": f": Fixture{index}"},
                },
                sort_keys=True,
            )
            + "\n"
        )
    path.write_text("".join(rows), encoding="utf-8")


def test_freeze_gate3_writes_exact_companion_partition(tmp_path: Path) -> None:
    mathlib = tmp_path / "mathlib.jsonl"
    sft = tmp_path / "sft.jsonl"
    _write_theorems(mathlib, "mathlib", 2)
    _write_theorems(sft, "sft_classic", 2)
    manifest_path = tmp_path / "gate3.json"

    path, digest = run_freeze_gate3_inputs(
        mathlib_jsonl=mathlib,
        sft_classic_jsonl=sft,
        out_path=manifest_path,
        per_source=1,
    )

    manifest = json.loads(path.read_text(encoding="utf-8"))
    partition = Path(manifest["theorem_partition"])
    rows = [json.loads(line) for line in partition.read_text(encoding="utf-8").splitlines()]
    assert digest == hash_file(manifest_path)
    assert manifest["schema_version"] == 2
    assert manifest["record_count"] == 2
    assert manifest["source_counts"] == {"mathlib": 1, "sft_classic": 1}
    assert manifest["context_id"] == "ctx:" + "0" * 64
    assert manifest["input_accounting"] == {
        "mathlib": {"input_records": 2, "eligible_records": 2, "selected_records": 1},
        "sft_classic": {"input_records": 2, "eligible_records": 2, "selected_records": 1},
    }
    assert manifest["theorem_partition_sha256"] == hash_file(partition)
    assert [row["theorem"]["source"] for row in rows] == ["mathlib", "sft_classic"]
    assert [row["theorem"]["theorem_id"] for row in rows] == [
        item["theorem_id"] for item in manifest["records"]
    ]
    assert all(row["representation"]["headless"].startswith(": Fixture") for row in rows)
    validated = _validate_frozen_gate3_manifest(manifest_path, partition)
    assert validated["record_count"] == 2

    partition.write_text(partition.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="partition hash mismatch"):
        _validate_frozen_gate3_manifest(manifest_path, partition)


def test_freeze_gate3_rejects_source_contamination(tmp_path: Path) -> None:
    mathlib = tmp_path / "mathlib.jsonl"
    sft = tmp_path / "sft.jsonl"
    _write_theorems(mathlib, "sft_classic", 1)
    _write_theorems(sft, "sft_classic", 1)

    with pytest.raises(ValueError, match="expected 'mathlib'"):
        run_freeze_gate3_inputs(
            mathlib_jsonl=mathlib,
            sft_classic_jsonl=sft,
            out_path=tmp_path / "gate3.json",
            per_source=1,
        )


def test_freeze_gate3_rejects_mixed_contexts_before_writing(tmp_path: Path) -> None:
    mathlib = tmp_path / "mathlib.jsonl"
    sft = tmp_path / "sft.jsonl"
    out = tmp_path / "gate3.json"
    _write_theorems(mathlib, "mathlib", 1)
    _write_theorems(sft, "sft_classic", 1, context_id="ctx:" + "1" * 64)

    with pytest.raises(ValueError, match="mixed contexts"):
        run_freeze_gate3_inputs(
            mathlib_jsonl=mathlib,
            sft_classic_jsonl=sft,
            out_path=out,
            per_source=1,
        )
    assert not out.exists()
    assert not out.with_suffix(".theorems.jsonl").exists()


def test_validate_gate3_manifest_rejects_tampered_accounting(tmp_path: Path) -> None:
    mathlib = tmp_path / "mathlib.jsonl"
    sft = tmp_path / "sft.jsonl"
    manifest_path = tmp_path / "gate3.json"
    _write_theorems(mathlib, "mathlib", 1)
    _write_theorems(sft, "sft_classic", 1)
    run_freeze_gate3_inputs(
        mathlib_jsonl=mathlib,
        sft_classic_jsonl=sft,
        out_path=manifest_path,
        per_source=1,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    partition = Path(manifest["theorem_partition"])
    manifest["source_counts"]["mathlib"] = 2
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="source_counts do not reconcile"):
        _validate_frozen_gate3_manifest(manifest_path, partition)


def test_merge_representation_partitions_preserves_chunk_order(tmp_path: Path) -> None:
    partitions = [tmp_path / "a", tmp_path / "b"]
    for index, partition in enumerate(partitions):
        records = partition / "records"
        failures = partition / "failures"
        records.mkdir(parents=True)
        failures.mkdir(parents=True)
        (records / "gate3.jsonl").write_text(f"record-{index}\n", encoding="utf-8")
        (failures / "gate3.jsonl").write_text(f"failure-{index}\n", encoding="utf-8")

    record_path, failure_path = _merge_representation_partitions(
        partitions,
        out_dir=tmp_path / "merged",
        source="gate3",
    )

    assert record_path.read_text(encoding="utf-8") == "record-0\nrecord-1\n"
    assert failure_path.read_text(encoding="utf-8") == "failure-0\nfailure-1\n"
    assert _merge_count_maps([{"theorems": 1}, {"theorems": 2, "failed": 1}]) == {
        "theorems": 3,
        "failed": 1,
    }


def test_inline_alias_infers_exact_environment_type() -> None:
    source = _inline_alias_of("lf_cross_path_0000", "Mathlib.Fixture.original")

    assert source == "noncomputable def lf_cross_path_0000 := @Mathlib.Fixture.original"


def test_cross_path_rejects_stale_named_normalization_version() -> None:
    theorem = theorem_record(
        source="mathlib",
        declaration_name="original",
        declaration_full_name="Mathlib.Fixture.original",
    )
    statuses = dict(representation_record().view_status)
    statuses["signature_explicit"] = ViewStatus.OK
    named = representation_record(
        theorem_id=theorem.theorem_id,
        normalization_version="repr_v1",
        signature_explicit="True",
        alpha_identity_fingerprint="a" * 64,
        view_status=statuses,
    )

    assert _cross_path_exclusion_reason(theorem, named) == "named_normalization_version_mismatch"


def test_cross_path_selection_is_public_deterministic_and_fully_accounted() -> None:
    def theorem(index: int, full_name: str):
        theorem_id = make_id("thm", {"cross_path": index})
        ancestry_id = make_id("anc", {"cross_path": index})
        return theorem_record(
            theorem_id=theorem_id,
            ancestry_id=ancestry_id,
            root_ancestry_ids=(ancestry_id,),
            source="mathlib",
            declaration_name=full_name.rsplit(".", maxsplit=1)[-1],
            declaration_full_name=full_name,
        )

    def representation(theorem_id: str, *, explicit: bool = True, alpha: bool = True):
        base = representation_record()
        statuses = dict(base.view_status)
        if explicit:
            statuses["signature_explicit"] = ViewStatus.OK
        return representation_record(
            representation_id=make_id("repr", {"cross_path": theorem_id}),
            theorem_id=theorem_id,
            signature_explicit="True" if explicit else None,
            alpha_identity_fingerprint="a" * 64 if alpha else None,
            view_status=statuses,
        )

    records = [
        theorem(0, "Public.first"),
        theorem(1, "_private.0.Hidden.secret"),
        theorem(2, "Public.noRepresentation"),
        theorem(3, "Public.noExplicit"),
        theorem(4, "Public.noAlpha"),
        theorem(5, "Public.second"),
        theorem(6, "Public.afterLimit"),
    ]
    representations = {
        records[0].theorem_id: representation(records[0].theorem_id),
        records[1].theorem_id: representation(records[1].theorem_id),
        records[3].theorem_id: representation(records[3].theorem_id, explicit=False),
        records[4].theorem_id: representation(records[4].theorem_id, alpha=False),
        records[5].theorem_id: representation(records[5].theorem_id),
        records[6].theorem_id: representation(records[6].theorem_id),
    }

    selected, exclusions = _select_cross_path_inputs(records, representations, cases=2)
    selected_again, exclusions_again = _select_cross_path_inputs(
        records,
        representations,
        cases=2,
    )

    assert [record.declaration_full_name for record in selected] == [
        "Public.first",
        "Public.second",
    ]
    assert selected_again == selected
    assert exclusions_again == exclusions
    assert len(selected) + len(exclusions) == len(records)
    assert {str(item["reason"]) for item in exclusions} == {
        "environment_only_private_name",
        "missing_named_representation",
        "missing_named_explicit_signature",
        "missing_named_alpha_identity_fingerprint",
        "case_limit_reached",
    }
    accounted_ids = {record.theorem_id for record in selected} | {
        str(item["theorem_id"]) for item in exclusions
    }
    assert accounted_ids == {record.theorem_id for record in records}


def test_completed_chunk_marker_is_content_bound(tmp_path: Path) -> None:
    _write_chunk_marker(tmp_path, job_hash="a" * 64, payload={"theorems": 20})
    assert _read_chunk_marker(tmp_path, expected_job_hash="a" * 64) == {"theorems": 20}
    with pytest.raises(ValueError, match="job hash mismatch"):
        _read_chunk_marker(tmp_path, expected_job_hash="b" * 64)

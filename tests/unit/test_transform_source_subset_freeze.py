"""Tests for the fail-closed Gate-3 source-subset freezer."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.schemas import RepresentationRecord, TheoremRecord, make_id
from leanfaith.transforms.source_subset_freeze import (
    SourceSubsetFreezeError,
    SourceSubsetFreezeManifest,
    freeze_transform_source_subset,
)
from tests.unit.record_factories import CTX_ID, representation_record, theorem_record


def _hex(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _records(
    source: str,
    index: int,
    *,
    context_id: str = CTX_ID,
) -> tuple[TheoremRecord, RepresentationRecord]:
    theorem_id = make_id("thm", {"source": source, "index": index})
    ancestry_id = make_id("anc", {"source": source, "index": index})
    theorem = theorem_record(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        source=source,
        source_record=f"{source}:{index}",
        context_id=context_id,
        declaration_name=f"t_{source}_{index}",
        proof_stripped_declaration=f"theorem t_{source}_{index} : True := by sorry",
        statement_content_hash=_hex(f"statement:{source}:{index}"),
    )
    representation = representation_record(
        representation_id=make_id("repr", {"theorem_id": theorem_id}),
        theorem_id=theorem_id,
        context_id=context_id,
        raw_proof_stripped=theorem.proof_stripped_declaration,
        headless=": True",
        signature_pp="True",
        content_hash=_hex(f"representation:{source}:{index}"),
    )
    return theorem, representation


def _write_inputs(
    directory: Path,
    pairs: list[tuple[TheoremRecord, RepresentationRecord]],
    *,
    wrapped_indices: set[int] | None = None,
) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    wrapped_indices = wrapped_indices or set()
    theorem_rows: list[dict[str, object]] = []
    representation_rows: list[dict[str, object]] = []
    for index, (theorem, representation) in enumerate(pairs):
        theorem_payload = theorem.model_dump(mode="json")
        if index in wrapped_indices:
            theorem_rows.append(
                {
                    "theorem": theorem_payload,
                    "representation": {"headless": representation.headless},
                }
            )
        else:
            theorem_rows.append(theorem_payload)
        representation_rows.append(representation.model_dump(mode="json"))

    theorem_path = directory / "input_theorems.jsonl"
    representation_path = directory / "input_representations.jsonl"
    theorem_path.write_bytes(b"".join(canonical_json_bytes(row) + b"\n" for row in theorem_rows))
    representation_path.write_bytes(
        b"".join(canonical_json_bytes(row) + b"\n" for row in representation_rows)
    )
    return theorem_path, representation_path


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_freezes_sorted_source_subset_and_replays_without_rewrite(tmp_path: Path) -> None:
    private_2 = _records("sft_classic", 2)
    public = _records("mathlib", 1)
    private_1 = _records("sft_classic", 1)
    theorem_path, representation_path = _write_inputs(
        tmp_path,
        [private_2, public, private_1],
        wrapped_indices={0, 2},
    )
    output_dir = tmp_path / "frozen"

    first = freeze_transform_source_subset(
        theorem_path=theorem_path,
        representation_path=representation_path,
        source="sft_classic",
        output_dir=output_dir,
    )

    assert first.record_count == 2
    assert first.replayed is False
    theorem_rows = _read_jsonl(first.theorem_path)
    theorem_ids = [row["theorem"]["theorem_id"] for row in theorem_rows]  # type: ignore[index]
    assert theorem_ids == sorted(theorem_ids)
    assert theorem_rows[0]["representation"] == {"headless": ": True"}
    representation_rows = _read_jsonl(first.representation_path)
    assert [row["theorem_id"] for row in representation_rows] == theorem_ids

    manifest = SourceSubsetFreezeManifest.model_validate_json(first.manifest_path.read_bytes())
    assert manifest.source == "sft_classic"
    assert manifest.record_count == 2
    assert manifest.input_record_count == 3
    assert manifest.theorem_output_sha256 == hash_file(first.theorem_path)
    assert manifest.representation_output_sha256 == hash_file(first.representation_path)

    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (first.theorem_path, first.representation_path, first.manifest_path)
    }
    second = freeze_transform_source_subset(
        theorem_path=theorem_path,
        representation_path=representation_path,
        source="sft_classic",
        output_dir=output_dir,
    )
    assert second.replayed is True
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (second.theorem_path, second.representation_path, second.manifest_path)
    } == before


def test_rejects_changed_preexisting_output(tmp_path: Path) -> None:
    theorem_path, representation_path = _write_inputs(tmp_path, [_records("sft_classic", 1)])
    output_dir = tmp_path / "frozen"
    artifacts = freeze_transform_source_subset(
        theorem_path=theorem_path,
        representation_path=representation_path,
        source="sft_classic",
        output_dir=output_dir,
    )
    artifacts.theorem_path.write_bytes(artifacts.theorem_path.read_bytes() + b"{}\n")

    with pytest.raises(SourceSubsetFreezeError, match="differs"):
        freeze_transform_source_subset(
            theorem_path=theorem_path,
            representation_path=representation_path,
            source="sft_classic",
            output_dir=output_dir,
        )


def test_rejects_non_aligned_theorem_and_representation_streams(tmp_path: Path) -> None:
    first = _records("sft_classic", 1)
    second = _records("mathlib", 2)
    theorem_path, representation_path = _write_inputs(tmp_path, [first, second])
    representations = _read_jsonl(representation_path)
    representation_path.write_bytes(
        b"".join(canonical_json_bytes(row) + b"\n" for row in reversed(representations))
    )

    with pytest.raises(SourceSubsetFreezeError, match="positionally one-to-one"):
        freeze_transform_source_subset(
            theorem_path=theorem_path,
            representation_path=representation_path,
            source="sft_classic",
            output_dir=tmp_path / "frozen",
        )


def test_rejects_duplicate_theorem_ids(tmp_path: Path) -> None:
    pair = _records("sft_classic", 1)
    theorem_path, representation_path = _write_inputs(tmp_path, [pair, pair])

    with pytest.raises(SourceSubsetFreezeError, match="duplicate theorem_id"):
        freeze_transform_source_subset(
            theorem_path=theorem_path,
            representation_path=representation_path,
            source="sft_classic",
            output_dir=tmp_path / "frozen",
        )


def test_rejects_duplicate_representation_ids(tmp_path: Path) -> None:
    first = _records("sft_classic", 1)
    second_theorem, second_representation = _records("mathlib", 2)
    second_representation = second_representation.model_copy(
        update={"representation_id": first[1].representation_id}
    )
    theorem_path, representation_path = _write_inputs(
        tmp_path, [first, (second_theorem, second_representation)]
    )

    with pytest.raises(SourceSubsetFreezeError, match="duplicate representation_id"):
        freeze_transform_source_subset(
            theorem_path=theorem_path,
            representation_path=representation_path,
            source="sft_classic",
            output_dir=tmp_path / "frozen",
        )


def test_rejects_mixed_contexts_and_empty_source_selection(tmp_path: Path) -> None:
    other_context = f"ctx:{'9' * 64}"
    theorem_path, representation_path = _write_inputs(
        tmp_path,
        [_records("sft_classic", 1), _records("mathlib", 2, context_id=other_context)],
    )
    with pytest.raises(SourceSubsetFreezeError, match="exactly one context_id"):
        freeze_transform_source_subset(
            theorem_path=theorem_path,
            representation_path=representation_path,
            source="sft_classic",
            output_dir=tmp_path / "mixed",
        )

    one_theorem, one_representation = _write_inputs(
        tmp_path / "empty_source", [_records("mathlib", 3)]
    )
    with pytest.raises(SourceSubsetFreezeError, match="no theorem records match"):
        freeze_transform_source_subset(
            theorem_path=one_theorem,
            representation_path=one_representation,
            source="sft_classic",
            output_dir=tmp_path / "empty",
        )


def test_cli_freezes_and_verifies_replay(tmp_path: Path) -> None:
    theorem_path, representation_path = _write_inputs(tmp_path, [_records("sft_classic", 1)])
    output_dir = tmp_path / "frozen"
    runner = CliRunner()
    arguments = [
        "freeze-transform-source-subset",
        "--theorems",
        str(theorem_path),
        "--representations",
        str(representation_path),
        "--source",
        "sft_classic",
        "--output-dir",
        str(output_dir),
    ]

    first = runner.invoke(app, arguments)
    assert first.exit_code == 0, first.output
    assert "records=1" in first.output
    assert "replayed=false" in first.output
    second = runner.invoke(app, arguments)
    assert second.exit_code == 0, second.output
    assert "replayed=true" in second.output

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from leanfaith.annotation_support import (
    EXACT_FRAME_ITEM_COUNT,
    EXACT_FRAME_RELATIVE_PATH,
    EXACT_FRAME_SHA256,
    AnnotationExportError,
    BlindingError,
    assert_blinded_payload,
    export_blinded_annotation_bundles,
)
from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.config.paths import find_repo_root

ROOT = find_repo_root(Path(__file__))
FRAME = ROOT / EXACT_FRAME_RELATIVE_PATH
ENTROPY = (bytes(range(32)), bytes(range(32, 64)))


def _jsonl(path: Path) -> list[dict[str, object]]:
    payload = path.read_bytes()
    assert payload.endswith(b"\n")
    rows = [json.loads(line) for line in payload.splitlines()]
    assert all(canonical_json_bytes(row) in payload for row in rows)
    return rows


def test_exact_frame_exports_two_reference_aware_blinded_bundles(tmp_path: Path) -> None:
    assert hash_file(FRAME) == EXACT_FRAME_SHA256
    run = export_blinded_annotation_bundles(
        repo_root=ROOT,
        frame_path=FRAME,
        output_root=tmp_path / "annotation_export",
        entropy_by_slot=ENTROPY,
    )

    first_rows = _jsonl(run.bundles[0].bundle_path)
    second_rows = _jsonl(run.bundles[1].bundle_path)
    assert len(first_rows) == len(second_rows) == EXACT_FRAME_ITEM_COUNT
    assert [row["opaque_item_token"] for row in first_rows] != [
        row["opaque_item_token"] for row in second_rows
    ]
    assert [row["natural_language_statement"] for row in first_rows] != [
        row["natural_language_statement"] for row in second_rows
    ]
    assert {
        (
            row["natural_language_statement"],
            json.dumps(row["lean_a"], sort_keys=True),
            json.dumps(row["lean_b"], sort_keys=True),
        )
        for row in first_rows
    } == {
        (
            row["natural_language_statement"],
            json.dumps(row["lean_a"], sort_keys=True),
            json.dumps(row["lean_b"], sort_keys=True),
        )
        for row in second_rows
    }

    expected_keys = {
        "opaque_item_token",
        "natural_language_statement",
        "lean_a",
        "lean_b",
        "permitted_context",
    }
    for row in (*first_rows, *second_rows):
        assert set(row) == expected_keys
        assert_blinded_payload(row)
        assert set(row["lean_a"]) == {"headless", "signature_pp", "signature_explicit"}
        assert set(row["lean_b"]) == {"headless", "signature_pp", "signature_explicit"}
        assert set(row["permitted_context"]) == {
            "minimal_import_text",
            "namespace_text",
            "local_notation_text",
            "required_type_information",
            "view_unavailable_notices",
        }
        assert row["permitted_context"]["minimal_import_text"] == "import Mathlib"
        visible_lean = (
            " ".join(str(value) for value in row["lean_a"].values())
            + " "
            + " ".join(str(value) for value in row["lean_b"].values())
        ).casefold()
        assert "lf021_research_" not in visible_lean
        assert "kimina_autoformalizer" not in visible_lean
        assert "goedel_formalizer" not in visible_lean
        assert "stepfun_formalizer" not in visible_lean

    assert run.bundles[0].manifest.bundle.sha256 == hash_file(run.bundles[0].bundle_path)
    assert run.bundles[1].manifest.bundle.sha256 == hash_file(run.bundles[1].bundle_path)
    assert run.private_manifest.source_frame.sha256 == EXACT_FRAME_SHA256
    assert run.private_manifest.private_linkage.sha256 == hash_file(run.private_linkage_path)
    linkage = _jsonl(run.private_linkage_path)
    assert len(linkage) == EXACT_FRAME_ITEM_COUNT
    assert len({str(row["source_frame_record_id"]) for row in linkage}) == 240
    assert len({str(row["target_pair_id"]) for row in linkage}) == 240
    assert {str(row["independent_annotator_1_item_id"]) for row in linkage} == {
        str(row["opaque_item_token"]) for row in first_rows
    }
    assert {str(row["independent_annotator_2_item_id"]) for row in linkage} == {
        str(row["opaque_item_token"]) for row in second_rows
    }
    assert stat.S_IMODE(run.private_linkage_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(run.private_manifest_path.stat().st_mode) == 0o600


def test_export_is_idempotent_for_identical_one_time_randomization(tmp_path: Path) -> None:
    kwargs = {
        "repo_root": ROOT,
        "frame_path": FRAME,
        "output_root": tmp_path / "annotation_export",
        "entropy_by_slot": ENTROPY,
    }
    first = export_blinded_annotation_bundles(**kwargs)
    second = export_blinded_annotation_bundles(**kwargs)
    assert first.private_manifest == second.private_manifest
    assert first.private_linkage_path.read_bytes() == second.private_linkage_path.read_bytes()
    assert first.bundles[0].bundle_path.read_bytes() == second.bundles[0].bundle_path.read_bytes()
    assert first.bundles[1].bundle_path.read_bytes() == second.bundles[1].bundle_path.read_bytes()


def test_equal_or_short_randomization_material_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(AnnotationExportError, match="independent randomization"):
        export_blinded_annotation_bundles(
            repo_root=ROOT,
            frame_path=FRAME,
            output_root=tmp_path / "same",
            entropy_by_slot=(b"x" * 32, b"x" * 32),
        )
    with pytest.raises(BlindingError, match="at least 32 bytes"):
        export_blinded_annotation_bundles(
            repo_root=ROOT,
            frame_path=FRAME,
            output_root=tmp_path / "short",
            entropy_by_slot=(b"x" * 31, b"y" * 32),
        )


def test_public_payload_leakage_scanner_rejects_lineage_and_outcomes() -> None:
    for forbidden in (
        "generator_id",
        "sampling_stratum",
        "sampling_seed_sha256",
        "score",
        "prior_votes",
        "same_claim",
        "relation",
        "frame_record_id",
        "theorem_a_id",
    ):
        with pytest.raises(BlindingError, match="forbidden annotation key"):
            assert_blinded_payload({"safe": {"nested": {forbidden: "hidden"}}})


def test_export_rejects_any_noncanonical_frame_path(tmp_path: Path) -> None:
    copied = tmp_path / FRAME.name
    copied.write_bytes(FRAME.read_bytes())
    with pytest.raises(AnnotationExportError, match="exact frozen frame path"):
        export_blinded_annotation_bundles(
            repo_root=ROOT,
            frame_path=copied,
            output_root=tmp_path / "out",
            entropy_by_slot=ENTROPY,
        )

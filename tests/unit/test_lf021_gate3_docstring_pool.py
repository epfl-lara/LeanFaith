"""Focused tests for the frozen Gate-3 mathlib docstring expansion."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import leanfaith.generation.gate3_docstring_pool as pool
from leanfaith.config.hashing import hash_file, sha256_hex
from leanfaith.config.loading import load_config
from leanfaith.config.paths import find_repo_root
from leanfaith.lean.extraction import PLACEHOLDER
from leanfaith.schemas.enums import ValidationStatus
from leanfaith.schemas.theorem import TheoremRecord

ROOT = find_repo_root(Path(__file__).parent)
MATHLIB = Path("/storage/milikic/leanfaith/mathlib4")


def _theorem(proof_stripped: str, *, source_range: tuple[int, int]) -> TheoremRecord:
    return TheoremRecord(
        theorem_id="thm:" + "1" * 64,
        ancestry_id="anc:" + "2" * 64,
        root_ancestry_ids=("anc:" + "2" * 64,),
        source="mathlib",
        source_revision="d568c8c09630de097a046763c17b9ea99f95f950",
        source_record="Mathlib/Test.lean",
        source_file="Mathlib/Test.lean",
        source_range=source_range,
        context_id="ctx:" + "3" * 64,
        declaration_kind="theorem",
        declaration_name="demo",
        declaration_full_name="Demo.demo",
        proof_stripped_declaration=proof_stripped,
        is_proposition=True,
        elaboration_status=ValidationStatus.ELABORATES,
        statement_content_hash="4" * 64,
        metadata={"transform_source_eligible": True},
    )


def test_extracts_exact_leading_docstring_with_attribute_docstring() -> None:
    header = (
        "/-- Primary contributor claim. -/\n"
        "@[to_additive /-- Secondary generated claim. -/]\n"
        "theorem demo : True"
    )
    source = header + " := by trivial\n"
    doc, observed_header = pool.extract_adjacent_docstring(
        theorem=_theorem(header + PLACEHOLDER, source_range=(1, 3)),
        source_text=source,
    )

    assert observed_header == header
    assert doc is not None
    assert doc.raw == "/-- Primary contributor claim. -/"
    assert doc.normalized_nl == "Primary contributor claim."
    assert doc.raw_sha256 == sha256_hex(doc.raw.encode())
    assert doc.start_line == doc.finish_line == 1


def test_missing_docstring_is_explicit_normal_exclusion() -> None:
    header = "@[simp] theorem demo : True"
    doc, observed_header = pool.extract_adjacent_docstring(
        theorem=_theorem(header + PLACEHOLDER, source_range=(1, 1)),
        source_text=header + " := by trivial\n",
    )

    assert doc is None
    assert observed_header == header


def test_theorem_source_drift_fails_closed() -> None:
    header = "/-- Claim. -/\ntheorem demo : True"
    theorem = _theorem(header + PLACEHOLDER, source_range=(1, 2))
    with pytest.raises(pool.Gate3DocstringPoolError, match="differs from pinned source"):
        pool.extract_adjacent_docstring(
            theorem=theorem,
            source_text="/-- Different. -/\ntheorem demo : True := by trivial\n",
        )


def test_unterminated_docstring_fails_closed() -> None:
    header = "/-- Claim.\ntheorem demo : True"
    theorem = _theorem(header + PLACEHOLDER, source_range=(1, 2))
    with pytest.raises(pool.Gate3DocstringPoolError, match="unterminated"):
        pool.extract_adjacent_docstring(
            theorem=theorem,
            source_text=header + " := by trivial\n",
        )


def test_config_binds_frozen_gate3_artifacts_and_no_model_or_label() -> None:
    config = load_config(ROOT / pool.CONFIG_PATH, pool.Gate3DocstringPoolConfig).config

    assert config.source.expected_mathlib_records == 5000
    assert config.selection.target_distinct_ancestry_groups == 300
    assert config.temporal_non_overlap.latest_checkpoint_created_at.isoformat() == (
        "2025-10-13T07:12:42+00:00"
    )
    assert len(config.temporal_non_overlap.checkpoint_pins) == 3
    assert config.policy.model_execution_performed is False
    assert config.policy.semantic_labels_created is False
    assert config.policy.self_containedness_status == "unreviewed"
    assert config.policy.problem_pool_admitted is False
    assert config.policy.model_collection_authorized is False
    assert config.policy.gate_claimed is False
    for artifact in (
        config.source.theorem_manifest,
        config.source.theorem_records,
        config.source.representation_records,
    ):
        if Path(artifact.path).is_file():
            assert hash_file(Path(artifact.path)) == artifact.sha256


@pytest.mark.skipif(
    not MATHLIB.is_dir()
    or not Path("/storage/milikic/leanfaith/gate3/frozen/gate3_inputs.theorems.jsonl").is_file(),
    reason="frozen Gate-3/mathlib artifacts unavailable",
)
def test_first_real_adjacent_docstring_preserves_frozen_reference() -> None:
    path = Path("/storage/milikic/leanfaith/gate3/frozen/gate3_inputs.theorems.jsonl")
    theorem: TheoremRecord | None = None
    for line in path.open(encoding="utf-8"):
        raw = json.loads(line)["theorem"]
        if raw["source"] == "mathlib" and raw["proof_stripped_declaration"].lstrip().startswith(
            "/--"
        ):
            theorem = TheoremRecord.model_validate(raw)
            break
    assert theorem is not None and theorem.source_file is not None
    source = (MATHLIB / theorem.source_file).read_text(encoding="utf-8")

    doc, header = pool.extract_adjacent_docstring(theorem=theorem, source_text=source)

    assert doc is not None
    assert doc.normalized_nl
    assert theorem.proof_stripped_declaration == header + PLACEHOLDER


def test_new_artifact_models_cannot_claim_model_labels_or_gate() -> None:
    candidate_fields = pool.Gate3MathlibDocstringCandidate.model_fields
    report_fields = pool.Gate3DocstringPoolReport.model_fields
    manifest_fields = pool.Gate3DocstringPoolManifest.model_fields

    for fields in (candidate_fields, report_fields, manifest_fields):
        assert fields["model_execution_performed"].default is False
        assert fields["semantic_labels_created"].default is False
        assert fields["gate_claimed"].default is False
        assert fields["problem_pool_admitted"].default is False
        assert fields["model_collection_authorized"].default is False


def test_exact_pair_check_rejects_prefix_name_collision() -> None:
    raw = "/-- Claim. -/"

    assert not pool._exact_pair_present(
        blob=raw + "\nlemma sum_eq_top_iff : True := by trivial\n",
        raw_docstring=raw,
        declaration_name="sum_eq_top",
    )
    assert pool._exact_pair_present(
        blob=raw + "\n@[simp] lemma sum_eq_top : True := by trivial\n",
        raw_docstring=raw,
        declaration_name="sum_eq_top",
    )


@pytest.mark.skipif(
    not (ROOT / pool.FULL_REPORT).is_file(),
    reason="full model-free docstring-pool preflight has not been materialized",
)
def test_persisted_full_pool_fails_closed_on_temporal_shortfall() -> None:
    report = pool.Gate3DocstringPoolReport.model_validate_json(
        (ROOT / pool.FULL_REPORT).read_text(encoding="utf-8")
    )
    manifest_path = Path(report.manifest.path)
    manifest = pool.Gate3DocstringPoolManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    selected_path = Path(report.selected_candidates.path)
    candidates = tuple(
        pool.Gate3MathlibDocstringCandidate.model_validate_json(line)
        for line in selected_path.read_text(encoding="utf-8").splitlines()
    )

    assert report.passed is False
    assert manifest.attempted_mathlib_records == 5000
    assert manifest.screen_clear_records == 533
    assert manifest.eligible_distinct_ancestry_groups == 57
    assert manifest.shortfall == 243
    assert len(candidates) == 57
    assert len({candidate.ancestry_id for candidate in candidates}) == 57
    assert all(
        candidate.temporal_introduction.introduction_created_at
        > candidate.temporal_introduction.latest_checkpoint_created_at
        and candidate.shared_three_family_temporal_eligible
        and not candidate.model_execution_performed
        and not candidate.semantic_labels_created
        and candidate.self_containedness_status == "unreviewed"
        and not candidate.problem_pool_admitted
        and not candidate.model_collection_authorized
        and not candidate.gate_claimed
        for candidate in candidates
    )

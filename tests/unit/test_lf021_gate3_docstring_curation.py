"""Tests for conservative operational curation of Gate-3 docstrings."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

import leanfaith.generation.gate3_docstring_curation as curation
from leanfaith.config.hashing import hash_file
from leanfaith.config.loading import load_config
from leanfaith.config.paths import find_repo_root
from leanfaith.generation.gate3_docstring_pool import Gate3MathlibDocstringCandidate
from leanfaith.schemas.theorem import RepresentationRecord

ROOT = find_repo_root(Path(__file__).parent)
ELIGIBLE = Path(
    "/storage/milikic/leanfaith/lf021/"
    "problem_pool_gate3_mathlib_docstrings_v1/full/eligible_candidates.jsonl"
)


def test_config_is_explicitly_nonhuman_nonsemantic_and_count_complete() -> None:
    config = load_config(
        ROOT / curation.CONFIG_PATH,
        curation.Gate3DocstringCurationConfig,
    ).config

    assert config.review.reviewer_type == "codex_agent"
    assert config.review.review_method == "llm_assisted_operational_curation_v1"
    assert config.review.human_reviewed is False
    assert config.review.semantic_gold_created is False
    assert config.review.default_decision is curation.CurationDecision.STANDALONE_SUFFICIENT
    assert len(config.review.exclusions) == 17
    assert config.expected_counts.reviewed == 57
    assert config.expected_counts.admitted == 40
    assert config.expected_counts.excluded == 17
    assert config.expected_counts.ambiguous_exclusions == 2
    assert config.policy.model_execution_performed is False
    assert config.policy.semantic_labels_created is False
    assert config.policy.gate_claimed is False


def test_all_frozen_bindings_match_when_available() -> None:
    config = load_config(
        ROOT / curation.CONFIG_PATH,
        curation.Gate3DocstringCurationConfig,
    ).config
    bindings = (
        config.inputs.candidate_manifest,
        config.inputs.eligible_candidates,
        config.inputs.upstream_report,
        config.inputs.theorem_records,
        config.inputs.representation_records,
        config.execution_context.import_header,
    )
    for binding in bindings:
        path = Path(binding.path)
        if not path.is_absolute():
            path = ROOT / path
        if path.is_file():
            assert hash_file(path) == binding.sha256


def test_reference_code_is_bound_to_exact_source_declaration() -> None:
    candidate = Gate3MathlibDocstringCandidate.model_construct(
        candidate_id="gate3_docstring_candidate:" + "a" * 64,
        declaration_full_name="Demo.source_theorem",
    )
    representation = RepresentationRecord.model_construct(
        signature_pp="∀ {α : Type u_2} {β : Type u_1}, α = α",
    )

    name, statement = curation._reference_code(
        candidate=candidate,
        representation=representation,
    )

    assert name == "LeanFaithCurationReference_" + "a" * 16
    assert statement == f"def {name} := @Demo.source_theorem\n"
    assert "sorry" not in statement


def test_operational_review_shape_separates_ambiguity_from_authorization() -> None:
    admitted = curation.OperationalReview(
        reviewer_type="codex_agent",
        review_method="llm_assisted_operational_curation_v1",
        decision=curation.CurationDecision.STANDALONE_SUFFICIENT,
        reason_code="standalone",
        rationale="The claim is sufficiently standalone for operational collection.",
        ambiguous_exclusion=False,
        model_collection_authorized=True,
        authorization_scope="local_models_only",
    )
    ambiguous = curation.OperationalReview(
        reviewer_type="codex_agent",
        review_method="llm_assisted_operational_curation_v1",
        decision=curation.CurationDecision.AMBIGUOUS_OPERATIONAL,
        reason_code="ambiguous",
        rationale="The wording is operationally ambiguous and is conservatively excluded.",
        ambiguous_exclusion=True,
        model_collection_authorized=False,
        authorization_scope="none",
    )

    assert admitted.model_collection_authorized
    assert admitted.reference_visible_to_generator is False
    assert ambiguous.ambiguous_exclusion
    assert not ambiguous.model_collection_authorized


@pytest.mark.skipif(
    not ELIGIBLE.is_file(), reason="frozen 57-record candidate artifact unavailable"
)
def test_frozen_curation_covers_every_candidate_once() -> None:
    config = load_config(
        ROOT / curation.CONFIG_PATH,
        curation.Gate3DocstringCurationConfig,
    ).config
    candidates = tuple(
        Gate3MathlibDocstringCandidate.model_validate_json(line)
        for line in ELIGIBLE.read_text(encoding="utf-8").splitlines()
    )
    exclusions = {item.candidate_id: item for item in config.review.exclusions}

    assert len(candidates) == 57
    assert set(exclusions) <= {item.candidate_id for item in candidates}
    reviews = tuple(curation._review_for(config=config, candidate=item) for item in candidates)
    counts = Counter(item.decision for item in reviews)
    assert counts[curation.CurationDecision.STANDALONE_SUFFICIENT] == 40
    assert sum(counts[item] for item in curation._EXCLUSION_DECISIONS) == 17
    assert counts[curation.CurationDecision.AMBIGUOUS_OPERATIONAL] == 2


@pytest.mark.skipif(
    not (ROOT / curation.REPORT_PATH).is_file(),
    reason="operational curation has not been materialized",
)
def test_persisted_curation_authorizes_only_kernel_bound_standalone_records() -> None:
    report = curation.CurationReport.model_validate_json(
        (ROOT / curation.REPORT_PATH).read_text(encoding="utf-8")
    )
    admitted = tuple(
        curation.OperationalCurationRecord.model_validate_json(line)
        for line in Path(report.admitted.path).read_text(encoding="utf-8").splitlines()
    )
    excluded = tuple(
        curation.OperationalCurationRecord.model_validate_json(line)
        for line in Path(report.excluded.path).read_text(encoding="utf-8").splitlines()
    )
    checks = json.loads(Path(report.reference_checks.path).read_text(encoding="utf-8"))

    assert report.passed
    assert len(admitted) == 40
    assert len(excluded) == 17
    assert checks["count"] == 40
    assert checks["all_valid"] is True
    assert checks["allow_sorry"] is False
    assert all(
        item.review.model_collection_authorized
        and item.review.authorization_scope == "local_models_only"
        and item.review.reference_visible_to_generator is False
        and item.reference is not None
        and item.reference.elaboration_status == "valid"
        and not item.semantic_labels_created
        and not item.human_review_claimed
        and not item.gate_claimed
        for item in admitted
    )
    assert all(
        not item.review.model_collection_authorized
        and item.reference is None
        and item.reference_context is None
        for item in excluded
    )

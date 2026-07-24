"""Contract tests for the model-free LF-021 cross-domain operational pool."""

from __future__ import annotations

from pathlib import Path

import pytest

import leanfaith.generation.cross_domain_operational_pool as operational
from leanfaith.config.hashing import hash_file
from leanfaith.config.loading import load_config
from leanfaith.config.paths import RepoPaths, find_repo_root
from leanfaith.generation.config import load_problem_pool_config
from leanfaith.schemas.nl_lean import ProblemPoolRecord

ROOT = find_repo_root(Path(__file__).parent)
OUTPUT = ROOT / "data/parsed/real_outputs/cross_domain_docstrings_operational_v1"
MANIFEST = OUTPUT / "problem_pool_manifest.json"


def _records() -> tuple[ProblemPoolRecord, ...]:
    return tuple(
        ProblemPoolRecord.model_validate_json(line)
        for line in (OUTPUT / "problem_pool_records.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    )


def test_source_and_pool_configs_are_fail_closed_and_hash_bound() -> None:
    source = load_config(
        ROOT / operational.SOURCE_CONFIG,
        operational.CrossDomainOperationalSourceConfig,
    ).config
    pool = load_problem_pool_config(ROOT / operational.POOL_CONFIG).config

    assert source.operational_curation.expected_admitted == 20
    assert source.operational_curation.expected_excluded == 4
    assert len(source.operational_curation.exclusions) == 4
    assert source.policy.model_collection_authorized is True
    assert source.policy.model_collection_scope == "local_models_only"
    assert source.policy.external_provider_collection_authorized is False
    assert source.policy.reference_visible_to_generator is False
    assert source.policy.human_review_claimed is False
    assert source.policy.semantic_labels_created is False
    assert source.policy.gate_claimed is False
    assert source.policy.model_execution_performed is False
    assert source.policy.generator_collection_plan_created is False
    assert source.policy.cross_domain_proxy_coverage_established is True
    assert source.policy.semantic_domain_gold_created is False

    enabled = tuple(item for item in pool.sources if item.enabled)
    assert len(enabled) == 1
    assert enabled[0].source == source.source
    assert enabled[0].source_config_sha256 == hash_file(ROOT / operational.SOURCE_CONFIG)
    assert enabled[0].external_provider_eligible is False


def test_configured_exclusions_are_referential_incomplete_or_malformed() -> None:
    source = load_config(
        ROOT / operational.SOURCE_CONFIG,
        operational.CrossDomainOperationalSourceConfig,
    ).config
    reasons = [item.reason_code for item in source.operational_curation.exclusions]
    assert reasons.count("referential_docstring") == 2
    assert reasons.count("incomplete_title_like_docstring") == 1
    assert reasons.count("malformed_model_visible_headless_view") == 1


@pytest.mark.skipif(not MANIFEST.is_file(), reason="operational pool is not materialized")
def test_persisted_pool_has_exact_fail_closed_proxy_accounting() -> None:
    manifest = operational.OperationalPoolManifest.model_validate_json(
        MANIFEST.read_text(encoding="utf-8")
    )
    assert manifest.reviewed_count == 24
    assert manifest.admitted_count == 20
    assert manifest.excluded_count == 4
    assert manifest.domain_proxy_counts == {
        "Analysis": 3,
        "Combinatorics": 4,
        "Geometry": 3,
        "NumberTheory": 4,
        "Probability": 2,
        "Topology": 4,
    }
    assert manifest.excluded_by_proxy == {
        "Analysis": 1,
        "Geometry": 1,
        "Probability": 2,
    }
    assert manifest.exclusion_reason_counts == {
        "incomplete_title_like_docstring": 1,
        "malformed_model_visible_headless_view": 1,
        "referential_docstring": 2,
    }
    assert manifest.cross_domain_proxy_coverage_established is True
    assert manifest.domain_proxy_is_semantic_gold is False
    assert manifest.semantic_domain_gold_created is False
    assert manifest.model_execution_performed is False
    assert manifest.semantic_labels_created is False
    assert manifest.gate_claimed is False


@pytest.mark.skipif(not MANIFEST.is_file(), reason="operational pool is not materialized")
def test_problem_records_are_local_only_reference_hidden_and_label_free() -> None:
    records = _records()
    assert len(records) == 20
    prohibited = {
        "reference_lean_statement",
        "reference_statement",
        "reference_type_pp",
        "signature_pp",
        "signature_explicit",
        "raw_proof_stripped",
    }
    assert all(
        item.eligibility == "eligible"
        and item.external_provider_eligible is False
        and len(item.reference_theorem_ids) == 1
        and item.metadata["model_collection_authorized"] is True
        and item.metadata["model_collection_scope"] == "local_models_only"
        and item.metadata["reference_visible_to_generator"] is False
        and item.metadata["semantic_gold_created"] is False
        and item.metadata["gate_claimed"] is False
        and item.metadata["domain_proxy_is_semantic_gold"] is False
        and item.metadata["semantic_domain_gold_created"] is False
        and not (prohibited & item.metadata.keys())
        for item in records
    )
    assert (OUTPUT / "problem_pool_failures.jsonl").read_bytes() == b""


@pytest.mark.skipif(not MANIFEST.is_file(), reason="operational pool is not materialized")
def test_exact_verify_replays_all_manifest_bindings() -> None:
    run = operational.verify_cross_domain_operational_pool(paths=RepoPaths.discover(ROOT))
    assert run.report.passed
    assert run.manifest.problem_count == 20
    for binding in (
        *run.manifest.input_artifacts.values(),
        *run.manifest.output_artifacts.values(),
    ):
        path = Path(binding.path)
        if not path.is_absolute():
            path = ROOT / path
        assert path.is_file()
        assert hash_file(path) == binding.sha256
    assert (
        sum(name.startswith("raw_reference_check:") for name in run.manifest.output_artifacts) == 20
    )

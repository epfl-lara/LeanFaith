"""Focused contract tests for the 40-record Gate-3 operational LF-021 pool."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

import leanfaith.generation.gate3_operational_pool as operational
from leanfaith.config.hashing import hash_file
from leanfaith.config.loading import load_config
from leanfaith.config.paths import find_repo_root
from leanfaith.generation.config import load_problem_pool_config
from leanfaith.schemas.nl_lean import ProblemPoolRecord
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord

ROOT = find_repo_root(Path(__file__).parent)
OUTPUT = ROOT / "data/parsed/real_outputs/gate3_docstrings_operational_v1"
MANIFEST = OUTPUT / "problem_pool_manifest.json"
STORAGE_CURATION = Path(
    "/storage/milikic/leanfaith/lf021/problem_pool_gate3_mathlib_docstrings_curation_v1"
)


def _jsonl(path: Path, model: type[ProblemPoolRecord]) -> tuple[ProblemPoolRecord, ...]:
    return tuple(
        model.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines()
    )


def _assert_binding(binding: operational.ArtifactBinding) -> None:
    path = Path(binding.path)
    if not path.is_absolute():
        path = ROOT / path
    assert path.is_file()
    assert hash_file(path) == binding.sha256


def test_operational_source_and_pool_are_explicitly_fail_closed() -> None:
    source = load_config(
        ROOT / operational.SOURCE_CONFIG,
        operational.OperationalSourceConfig,
    ).config
    pool = load_problem_pool_config(ROOT / operational.POOL_CONFIG).config

    assert source.policy.expected_admitted_problem_records == 40
    assert source.policy.domain == "Algebra"
    assert source.policy.domain_proxy_is_semantic_gold is False
    assert source.policy.cross_domain_diversity_established is False
    assert source.policy.model_collection_authorized is True
    assert source.policy.model_collection_scope == "local_models_only"
    assert source.policy.external_provider_collection_authorized is False
    assert source.policy.reference_visible_to_generator is False
    assert source.policy.human_review_claimed is False
    assert source.policy.semantic_labels_created is False
    assert source.policy.gate_claimed is False
    assert source.policy.model_execution_performed is False
    assert source.policy.generator_collection_plan_created is False
    assert source.policy.recovery_parser_binding_status == "unresolved"

    enabled = tuple(item for item in pool.sources if item.enabled)
    assert len(enabled) == 1
    assert enabled[0].source == source.source
    assert enabled[0].source_config_sha256 == hash_file(ROOT / operational.SOURCE_CONFIG)
    assert enabled[0].external_provider_eligible is False


@pytest.mark.parametrize(
    ("source_file", "expected"),
    [
        (
            "Mathlib/Algebra/Group/Defs.lean",
            ("Algebra", "Algebra/Group", "Group"),
        ),
        (
            "Mathlib/Algebra/BigOperators/Finprod.lean",
            ("Algebra", "Algebra/BigOperators", "BigOperators"),
        ),
        (
            "Mathlib/Algebra/DualNumber.lean",
            ("Algebra", "Algebra/other", "other"),
        ),
        (
            "Mathlib/Algebra/CharP/LinearMaps.lean",
            ("Algebra", "Algebra/other", "other"),
        ),
    ],
)
def test_domain_proxy_is_a_deterministic_path_bucket(
    source_file: str,
    expected: tuple[str, str, str],
) -> None:
    assert operational._domain_and_proxies(source_file) == expected


def test_domain_proxy_rejects_a_non_algebra_source() -> None:
    with pytest.raises(operational.Gate3OperationalPoolError, match="Algebra"):
        operational._domain_and_proxies("Mathlib/Topology/Basic.lean")


@pytest.mark.skipif(not MANIFEST.is_file(), reason="operational pool is not materialized")
def test_persisted_manifest_has_exact_counts_and_nonsemantic_domain_proxies() -> None:
    manifest = operational.OperationalPoolManifest.model_validate_json(
        MANIFEST.read_text(encoding="utf-8")
    )

    assert manifest.problem_count == 40
    assert manifest.domain == "Algebra"
    assert manifest.domain_proxy_counts == {
        "Algebra/AffineMonoid": 4,
        "Algebra/Algebra": 4,
        "Algebra/BigOperators": 9,
        "Algebra/Category": 5,
        "Algebra/Exact": 3,
        "Algebra/Group": 11,
        "Algebra/other": 4,
    }
    assert manifest.subdomain_proxy_counts == {
        "AffineMonoid": 4,
        "Algebra": 4,
        "BigOperators": 9,
        "Category": 5,
        "Exact": 3,
        "Group": 11,
        "other": 4,
    }
    assert manifest.domain_proxy_is_semantic_gold is False
    assert manifest.cross_domain_diversity_established is False
    assert manifest.model_collection_authorized_count == 40
    assert manifest.reference_visible_to_generator is False
    assert manifest.human_reviewed is False
    assert manifest.semantic_gold_created is False
    assert manifest.gate_claimed is False
    assert manifest.model_execution_performed is False
    assert manifest.generator_collection_plan_created is False
    assert manifest.recovery_parser_binding_status == "unresolved"

    invalid = manifest.model_dump(mode="json")
    invalid["subdomain_proxy_counts"] = {"Group": 39}
    with pytest.raises(ValidationError, match="subdomain-proxy counts"):
        operational.OperationalPoolManifest.model_validate(invalid)


@pytest.mark.skipif(not MANIFEST.is_file(), reason="operational pool is not materialized")
def test_persisted_records_are_canonical_screened_and_reference_hidden() -> None:
    manifest = operational.OperationalPoolManifest.model_validate_json(
        MANIFEST.read_text(encoding="utf-8")
    )
    records = _jsonl(OUTPUT / "problem_pool_records.jsonl", ProblemPoolRecord)
    theorems = tuple(
        TheoremRecord.model_validate_json(line)
        for line in (OUTPUT / "reference_theorems.jsonl").read_text(encoding="utf-8").splitlines()
    )
    representations = tuple(
        RepresentationRecord.model_validate_json(line)
        for line in (OUTPUT / "reference_representations.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    audits = tuple(
        operational.OperationalPoolRecordAudit.model_validate_json(line)
        for line in (OUTPUT / "record_audits.jsonl").read_text(encoding="utf-8").splitlines()
    )

    assert len(records) == len(theorems) == len(representations) == len(audits) == 40
    assert (OUTPUT / "problem_pool_failures.jsonl").read_bytes() == b""
    assert {item.problem_record_id for item in records} == set(manifest.problem_record_ids)
    assert {item.theorem_id for item in theorems} == set(manifest.theorem_ids)
    assert {item.representation_id for item in representations} == set(manifest.representation_ids)
    assert all(
        item.eligibility == "eligible"
        and item.external_provider_eligible is False
        and len(item.reference_theorem_ids) == 1
        and item.metadata["model_collection_authorized"] is True
        and item.metadata["model_collection_scope"] == "local_models_only"
        and item.metadata["reference_visible_to_generator"] is False
        and item.metadata["semantic_gold_created"] is False
        and item.metadata["gate_claimed"] is False
        and item.metadata["domain"] == "Algebra"
        and item.metadata["domain_proxy_is_semantic_gold"] is False
        and item.metadata["cross_domain_diversity_established"] is False
        for item in records
    )
    prohibited_reference_fields = {
        "reference_lean_statement",
        "reference_statement",
        "reference_type_pp",
        "signature_pp",
        "signature_explicit",
        "raw_proof_stripped",
    }
    assert all(not (prohibited_reference_fields & item.metadata.keys()) for item in records)
    assert all(
        item.registry_screens.all_three_screens_clear
        and item.no_sorry_alias_check_valid
        and item.model_collection_authorized
        and item.reference_visible_to_generator is False
        and item.semantic_gold_created is False
        and item.gate_claimed is False
        for item in audits
    )


@pytest.mark.skipif(not MANIFEST.is_file(), reason="operational pool is not materialized")
def test_manifest_and_reports_bind_every_materialized_artifact() -> None:
    manifest = operational.OperationalPoolManifest.model_validate_json(
        MANIFEST.read_text(encoding="utf-8")
    )
    report = operational.OperationalPoolReport.model_validate_json(
        (ROOT / operational.REPORT_PATH).read_text(encoding="utf-8")
    )
    adequacy = operational.OperationalSourceAdequacyReport.model_validate_json(
        (ROOT / operational.ADEQUACY_REPORT_PATH).read_text(encoding="utf-8")
    )

    for binding in (
        manifest.source_config_artifact,
        manifest.curation_config_artifact,
        manifest.curation_report_artifact,
        manifest.curation_manifest_artifact,
        manifest.curation_admitted_artifact,
        manifest.no_sorry_reference_checks_artifact,
        manifest.problem_records_artifact,
        manifest.context_artifact,
        manifest.reference_theorems_artifact,
        manifest.reference_representations_artifact,
        manifest.record_audits_artifact,
        manifest.import_header_artifact,
        manifest.active_benchmark_manifest_artifact,
    ):
        _assert_binding(binding)

    assert report.manifest_sha256 == hash_file(MANIFEST)
    assert report.adequacy_report_sha256 == hash_file(ROOT / operational.ADEQUACY_REPORT_PATH)
    assert report.problem_record_count == report.eligible_problem_count == 40
    assert report.model_collection_authorized_count == 40
    assert adequacy.passed
    assert adequacy.domain == "Algebra"
    assert adequacy.cross_domain_diversity_established is False
    assert adequacy.model_collection_authorized
    assert adequacy.generator_collection_plan_created is False
    assert adequacy.recovery_parser_binding_status == "unresolved"
    assert not any("plan" in path.name for path in OUTPUT.iterdir())


@pytest.mark.skipif(
    not STORAGE_CURATION.is_dir(),
    reason="frozen operational curation artifacts are unavailable",
)
def test_all_40_no_sorry_alias_checks_remain_bound_and_valid() -> None:
    source = load_config(
        ROOT / operational.SOURCE_CONFIG,
        operational.OperationalSourceConfig,
    ).config
    checks_path = Path(source.operational_curation.no_sorry_reference_checks.path)
    checks = operational.NoSorryReferenceChecks.model_validate_json(
        checks_path.read_text(encoding="utf-8")
    )

    assert checks.count == 40
    assert checks.allow_sorry is False
    assert checks.all_valid is True
    assert len(checks.checks) == 40
    for check in checks.checks:
        _assert_binding(check.raw_response_artifact)

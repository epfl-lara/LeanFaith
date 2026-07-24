"""Tests for the bounded non-Algebra LF-021 feasibility probe."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from leanfaith.config.hashing import hash_file
from leanfaith.config.loading import load_config
from leanfaith.config.paths import RepoPaths, find_repo_root
from leanfaith.generation import cross_domain_docstring_probe as probe

ROOT = find_repo_root(Path(__file__).parent)


def test_config_is_bounded_public_model_free_and_cross_domain() -> None:
    config = load_config(ROOT / probe.CONFIG_PATH, probe.CrossDomainProbeConfig).config

    assert len(config.domains) == 6
    assert config.selection.minimum_domain_proxies == 4
    assert {item.domain_proxy for item in config.domains} == {
        "Analysis",
        "Combinatorics",
        "Geometry",
        "NumberTheory",
        "Probability",
        "Topology",
    }
    cutoff = config.source.latest_generator_checkpoint_created_at
    for item in config.domains:
        parts = PurePosixPath(item.source_file).parts
        assert parts[:2] == ("Mathlib", item.domain_proxy)
        assert item.domain_proxy != "Algebra"
        assert item.file_addition_created_at > cutoff
        assert item.screening_limit >= item.target_selected
    assert config.policy.source_license == "Apache-2.0"
    assert config.policy.domain_semantics == "top_level_mathlib_directory_proxy_only"
    assert config.policy.problem_pool_admitted is False
    assert config.policy.model_collection_authorized is False
    assert config.policy.model_execution_performed is False
    assert config.policy.semantic_labels_created is False
    assert config.policy.private_source_content_used is False
    assert config.policy.external_provider_transmission_performed is False
    assert config.policy.gate_claimed is False


def test_config_binds_registry_header_and_available_source_bytes() -> None:
    config = load_config(ROOT / probe.CONFIG_PATH, probe.CrossDomainProbeConfig).config

    registry = ROOT / config.screening.active_registry_manifest
    assert hash_file(registry) == config.screening.active_registry_manifest_sha256
    header = ROOT / config.source.import_header.path
    assert hash_file(header) == config.source.import_header.sha256
    checkout = Path("/storage/milikic/leanfaith/mathlib4")
    if checkout.is_dir():
        for item in config.domains:
            assert hash_file(checkout / item.source_file) == item.source_file_sha256


def test_domain_accounting_rejects_inverted_funnel() -> None:
    with pytest.raises(ValueError, match="wrong direction"):
        probe.DomainAccounting(
            source_file="Mathlib/Analysis/Test.lean",
            declarations_seen=10,
            proposition_references=5,
            adjacent_docstrings=6,
            normalized_nl_clear=4,
            bounded_reference_screens=4,
            representation_complete=4,
            registry_clear=4,
            temporally_clean=4,
            selected=4,
            target_selected=4,
            target_met=True,
        )


def test_selected_outcome_requires_candidate_id() -> None:
    with pytest.raises(ValueError, match="requires candidate_id"):
        probe.ProbeOutcome(
            theorem_id="thm:" + "1" * 64,
            domain_proxy="Topology",
            declaration_full_name="Demo.claim",
            outcome=probe.ProbeOutcomeCode.SELECTED,
            detail="selected",
        )


@pytest.mark.skipif(
    not (ROOT / probe.REPORT_PATH).is_file(),
    reason="cross-domain feasibility artifact has not been materialized",
)
def test_persisted_probe_replays_and_makes_no_admission_or_gate_claim() -> None:
    run = probe.verify_cross_domain_probe(paths=RepoPaths.discover(ROOT))

    assert run.report.passed
    assert run.report.selected_count == 24
    assert len(run.report.selected_domain_proxies) == 6
    assert run.manifest.proposition_references == 190
    assert run.manifest.terminal_outcomes == 190
    assert run.manifest.declarations_seen == 224
    assert run.manifest.extraction_failures == 34
    assert run.manifest.selected_candidates == 24
    assert run.manifest.selected_domain_proxies == 6
    assert all(item.selected == 4 for item in run.manifest.domain_accounting.values())
    for record in (run.report, run.manifest):
        assert record.problem_pool_admitted is False
        assert record.model_collection_authorized is False
        assert record.model_execution_performed is False
        assert record.semantic_labels_created is False
        assert record.gate_claimed is False

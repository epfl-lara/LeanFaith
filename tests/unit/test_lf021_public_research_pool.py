"""Fail-closed tests for the model-free LF-021 public research pool."""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

import leanfaith.generation.public_research_pool as public_pool
from leanfaith.config.hashing import hash_file
from leanfaith.config.loading import load_config, load_yaml_mapping
from leanfaith.config.paths import RepoPaths, find_repo_root
from leanfaith.generation.config import load_problem_pool_config
from leanfaith.generation.public_research_pool import (
    ActiveRegistryScreens,
    LocalResearchSourceMatrix,
    PublicResearchPoolError,
    PublicResearchPoolManifest,
    PublicResearchPoolReport,
    PublicResearchSourceManifest,
)

ROOT = find_repo_root(Path(__file__).parent)
PATHS = RepoPaths(ROOT)
SOURCE_REPO = Path("/storage/milikic/leanfaith/mathlib4")


def _source_manifest() -> PublicResearchSourceManifest:
    payload = json.loads((ROOT / public_pool.SOURCE_MANIFEST).read_text(encoding="utf-8"))
    return PublicResearchSourceManifest.model_validate(payload)


def test_ready_pool_binds_exact_public_source_authorization_and_registry() -> None:
    loaded = load_problem_pool_config(ROOT / public_pool.POOL_CONFIG)
    config = loaded.config
    source = config.sources[0]

    assert config.status == "ready"
    assert len(config.sources) == 1
    assert source.enabled is True
    assert source.private_source is False
    assert source.external_provider_eligible is True
    assert source.allowed_trust == ("trusted",)
    assert source.source_config_sha256 == hash_file(ROOT / source.source_config)
    assert config.private_source_external_transmission is False

    public_pool._validate_ready_pool_source(paths=PATHS, config=config)


def test_public_source_records_preserve_conservative_overlap_status() -> None:
    manifest = _source_manifest()

    assert len(manifest.records) == 3
    assert manifest.frozen_at <= datetime.datetime.now(tz=datetime.UTC)
    assert manifest.records[0].nl_statement == ("For 0 ≤ x we have x - x ^ 3 / 6 ≤ sin x.")
    assert "best possible" not in manifest.records[0].nl_statement
    for record in manifest.records:
        assert record.formalrx_source_lineage_tag == ("mathlib_docstring_theorem_pairs")
        assert record.temporal_provenance == "post_submission_commit"
        assert record.pretraining_contamination_status == "unknown"
        assert record.reference_equivalence_to_source_status == (
            "textually_derived_cross_elaborated_not_kernel_compared_across_snapshot"
        )
        assert record.nl_claim_span in record.docstring_block
        assert (
            " ".join(record.nl_claim_span.replace("**", "").replace("`", "").split())
            == record.nl_statement
        )
        assert "unseen" not in record.model_dump_json().lower()


def test_source_record_identity_separates_locator_from_content() -> None:
    manifest = _source_manifest()
    record = manifest.records[0]
    source_id, content_hash = public_pool._source_record_identity(manifest, record)
    changed = record.model_copy(update={"nl_statement": record.nl_statement + " "})
    changed_id, changed_content_hash = public_pool._source_record_identity(manifest, changed)

    assert source_id == changed_id
    assert content_hash != changed_content_hash
    assert len(source_id) == len(content_hash) == 64


@pytest.mark.skipif(
    not (SOURCE_REPO / ".git").exists(),
    reason="public mathlib Git object store unavailable",
)
def test_all_three_docstring_signature_pairs_have_exact_git_provenance() -> None:
    manifest = _source_manifest()
    source_config = load_yaml_mapping(ROOT / public_pool.SOURCE_CONFIG)

    audits = tuple(
        public_pool._verify_source_provenance(
            repo=SOURCE_REPO,
            manifest=manifest,
            record=record,
            source_config=source_config,
        )
        for record in manifest.records
    )

    assert len(audits) == 3
    assert all(audit.introduction_is_first_text_occurrence for audit in audits)
    assert all(audit.docstring_immediately_precedes_signature for audit in audits)
    assert all(audit.pair_present_in_snapshot for audit in audits)


def test_local_source_matrix_has_three_distinct_disabled_candidates() -> None:
    matrix = load_config(ROOT / public_pool.SOURCE_MATRIX, LocalResearchSourceMatrix).config

    assert matrix.status == "pool_compatible_generation_disabled"
    assert matrix.semantic_labels_created is False
    assert matrix.gate_5g_credit_authorized is False
    assert len({family.family_id for family in matrix.families}) == 3
    assert all(family.scientific_activation.startswith("disabled_") for family in matrix.families)
    assert matrix.heldout.supervision_eligible is False


def test_three_active_registry_screens_must_all_be_clear() -> None:
    clear = ActiveRegistryScreens(
        problem_identity_and_nl_hits=(),
        reference_lean_text_hits=(),
        reference_representation_hits=(),
        all_three_screens_clear=True,
        registry_manifest_sha256="a" * 64,
        active_registry_sha256="b" * 64,
        registry_content_hash="c" * 64,
    )
    assert clear.all_three_screens_executed is True

    with pytest.raises(ValueError, match="does not match hit sets"):
        ActiveRegistryScreens(
            problem_identity_and_nl_hits=("normalized_nl:x",),
            reference_lean_text_hits=(),
            reference_representation_hits=(),
            all_three_screens_clear=True,
            registry_manifest_sha256="a" * 64,
            active_registry_sha256="b" * 64,
            registry_content_hash="c" * 64,
        )


def test_full_slice_cannot_run_before_one_example_artifact(tmp_path: Path) -> None:
    with pytest.raises(
        PublicResearchPoolError,
        match="requires the one-example preflight artifact first",
    ):
        public_pool._validate_one_example_report(
            paths=RepoPaths(tmp_path),
            source_manifest_hash="a" * 64,
            first_problem_id="example",
        )


def test_report_cannot_claim_gate_credit_or_model_execution() -> None:
    fields = PublicResearchPoolReport.model_fields

    assert fields["model_execution_performed"].default is False
    assert fields["semantic_labels_created"].default is False
    assert fields["private_source_transmission_performed"].default is False
    assert fields["gate_5g_closed"].default is False
    assert fields["gate_5_closed"].default is False


def test_persisted_one_then_three_record_artifacts_reconcile() -> None:
    one_report_path = ROOT / public_pool.ONE_EXAMPLE_REPORT
    full_report_path = ROOT / "reports/generation/lf021_public_research_pool_v1.json"
    one_manifest_path = ROOT / public_pool.ONE_EXAMPLE_OUTPUT / "problem_pool_manifest.json"
    full_manifest_path = (
        ROOT / "data/parsed/real_outputs/public_research_v1/problem_pool_manifest.json"
    )
    one_report = PublicResearchPoolReport.model_validate_json(
        one_report_path.read_text(encoding="utf-8")
    )
    full_report = PublicResearchPoolReport.model_validate_json(
        full_report_path.read_text(encoding="utf-8")
    )
    one_manifest = PublicResearchPoolManifest.model_validate_json(
        one_manifest_path.read_text(encoding="utf-8")
    )
    full_manifest = PublicResearchPoolManifest.model_validate_json(
        full_manifest_path.read_text(encoding="utf-8")
    )

    assert one_report.passed is True
    assert one_report.source_record_count == 1
    assert one_manifest.source_record_count == 1
    assert full_report.passed is True
    assert full_report.source_record_count == 3
    assert full_manifest.source_record_count == 3
    assert full_report.one_example_preflight_artifact == str(public_pool.ONE_EXAMPLE_REPORT)
    assert full_report.one_example_preflight_sha256 == hash_file(one_report_path)
    assert one_report.manifest_sha256 == hash_file(one_manifest_path)
    assert full_report.manifest_sha256 == hash_file(full_manifest_path)
    assert one_manifest.runtime_lean_version_guard_passed is True
    assert full_manifest.runtime_lean_version_guard_passed is True
    assert all(
        audit.active_registry_screens.all_three_screens_clear
        and audit.pretraining_contamination_status == "unknown"
        for audit in full_report.record_audits
    )

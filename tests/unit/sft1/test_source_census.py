"""Lean-free invariants for the incomplete SFT1 Wave 1 source census."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from leanfaith.config.hashing import hash_file as real_hash_file
from leanfaith.config.loading import DuplicateKeyError, load_config, load_yaml_mapping
from leanfaith.sft1 import source_census
from leanfaith.sft1.source_census import (
    EXPECTED_CONFIG_FILE_SHA256,
    EXPECTED_CONFIG_HASH,
    EXPECTED_OPERATIONS,
    EXPECTED_PROJECTS,
    SourceCensusError,
    Wave1SourceCensus,
    load_wave1_source_census,
    n31_n_proof_project_eligibility,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = (
    REPO_ROOT / "configs/transformations/sft1_value_first_v1/wave1_source_census_v0_3_2.yaml"
)


def _payload() -> dict[str, Any]:
    return copy.deepcopy(load_yaml_mapping(CONFIG_PATH))


def _source(payload: dict[str, Any], source_id: str) -> dict[str, Any]:
    return next(item for item in payload["sources"] if item["source_id"] == source_id)


def test_loads_exact_design_freeze_and_replays_hashes() -> None:
    loaded = load_wave1_source_census(REPO_ROOT)
    assert loaded.config_hash == EXPECTED_CONFIG_HASH
    assert loaded.config_file_sha256 == EXPECTED_CONFIG_FILE_SHA256
    assert tuple(item.source_id for item in loaded.config.sources) == EXPECTED_PROJECTS
    assert loaded.config.policy_binding.selected_operation_ids == EXPECTED_OPERATIONS


def test_design_freeze_authorizes_no_execution_or_receipt() -> None:
    config = load_wave1_source_census(REPO_ROOT).config
    assert config.status == "design_only_incomplete"
    assert config.authorization.executes_lean is False
    assert config.authorization.may_start_lean is False
    assert config.authorization.may_execute_transforms is False
    assert config.authorization.may_generate_rows is False
    assert config.authorization.may_write_passed_receipt is False
    assert config.authorization.row_commitment_authorized is False
    assert config.authorization.ten_k_authorized is False
    assert config.completion.census_passed is False
    assert config.completion.receipt_path is None
    assert config.completion.measured_census_counts_present is False


def test_loader_touches_only_repo_relative_small_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[Path] = []

    def recording_hash(path: Path) -> str:
        observed.append(path.resolve())
        return real_hash_file(path)

    monkeypatch.setattr(source_census, "hash_file", recording_hash)
    load_wave1_source_census(REPO_ROOT)
    assert observed
    assert all(path.is_relative_to(REPO_ROOT.resolve()) for path in observed)
    assert not any(str(path).startswith("/storage/") for path in observed)


def test_source_routes_distinguish_signatures_from_imported_constants() -> None:
    config = load_wave1_source_census(REPO_ROOT).config
    by_id = {source.source_id: source for source in config.sources}
    compiler = by_id["compiler_data"]
    assert compiler.source_kind == "extracted_signature"
    assert compiler.closed_expr.route == (
        "persistent_term_elab_of_signature_and_complete_telescope_without_declaration"
    )
    assert compiler.closed_expr.input_kind == "source_faithful_signature_text"
    for source_id in ("cslib", "mathlib", "physlib"):
        library = by_id[source_id]
        assert library.source_kind == "imported_constant"
        assert library.closed_expr.route == (
            "constant_info_type_with_canonical_universe_instantiation"
        )
        assert library.closed_expr.input_kind == "imported_constant_name"
    assert all(not source.closed_expr.declaration_insertion_allowed for source in config.sources)
    assert all(
        not source.closed_expr.pretty_print_reelaboration_allowed for source in config.sources
    )


def test_precursor_row_counts_are_never_claimed_as_census_counts() -> None:
    config = load_wave1_source_census(REPO_ROOT).config
    precursors = [item for source in config.sources for item in source.identity.external_precursors]
    assert precursors
    assert all(item.reported_rows > 0 for item in precursors)
    assert all(item.reported_rows_are_sft1_census_counts is False for item in precursors)
    assert all(not source.census_measurements.populated() for source in config.sources)
    assert all(
        source.signature_inventory.raw_theorem_or_lemma_count is None for source in config.sources
    )


def test_cluster_contract_is_cross_source_and_fail_closed() -> None:
    contract = load_wave1_source_census(REPO_ROOT).config.cluster_contract
    assert contract.preserve_exact_clusters_intact is True
    assert contract.preserve_near_duplicate_clusters_intact is True
    assert contract.cross_source_cluster_union_required is True
    assert contract.precursor_group_counts_are_census_counts is False
    assert contract.missing_cluster_input_is_eligible is False


def test_mathlib_cluster_artifacts_are_explicitly_partial() -> None:
    config = load_wave1_source_census(REPO_ROOT).config
    mathlib = next(source for source in config.sources if source.source_id == "mathlib")
    assert mathlib.signature_inventory.status == "partial_precursor"
    assert mathlib.signature_inventory.completeness == "partial"
    assert mathlib.cluster_inputs.exact_status == "partial_precursor"
    assert mathlib.cluster_inputs.near_status == "partial_precursor"
    assert mathlib.cluster_inputs.sft1_exact_cluster_count is None
    assert mathlib.cluster_inputs.sft1_near_duplicate_cluster_count is None


def test_n31_n_proof_is_ineligible_for_every_project() -> None:
    config = load_wave1_source_census(REPO_ROOT).config
    assert n31_n_proof_project_eligibility(config) == {
        "compiler_data": False,
        "cslib": False,
        "mathlib": False,
        "physlib": False,
    }
    for source in config.sources:
        assert source.n31_source_proof.status == "unknown"
        n31 = source.operation_eligibility[-1]
        assert n31.operation_id == "N31_DROP_REQUIRED_GUARD_PROOF_V1"
        assert n31.eligible is False
        assert n31.reason == "source_proof_unknown"


def test_all_wave_operation_source_eligibility_remains_closed() -> None:
    config = load_wave1_source_census(REPO_ROOT).config
    for source in config.sources:
        assert (
            tuple(item.operation_id for item in source.operation_eligibility) == EXPECTED_OPERATIONS
        )
        assert all(item.eligible is False for item in source.operation_eligibility)


def test_unknown_field_is_rejected() -> None:
    payload = _payload()
    payload["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Wave1SourceCensus.model_validate(payload)


def test_duplicate_yaml_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.yaml"
    path.write_text(CONFIG_PATH.read_text() + "\nschema_version: 1\n", encoding="utf-8")
    with pytest.raises(DuplicateKeyError, match="duplicate key"):
        load_config(path, Wave1SourceCensus)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("census_passed", True),
        ("measured_census_counts_present", True),
        ("wave1_source_eligibility_complete", True),
    ],
)
def test_completion_cannot_be_claimed_without_a_new_schema(field: str, value: bool) -> None:
    payload = _payload()
    payload["completion"][field] = value
    with pytest.raises(ValidationError):
        Wave1SourceCensus.model_validate(payload)


def test_passed_receipt_path_cannot_be_added() -> None:
    payload = _payload()
    payload["completion"]["receipt_path"] = "unearned.json"
    with pytest.raises(ValidationError):
        Wave1SourceCensus.model_validate(payload)


def test_measured_count_is_rejected_in_design_only_file() -> None:
    payload = _payload()
    _source(payload, "mathlib")["census_measurements"]["raw_theorem_or_lemma_count"] = 27786
    with pytest.raises(ValidationError, match="cannot claim measured census counts"):
        Wave1SourceCensus.model_validate(payload)


def test_upstream_precursor_count_cannot_be_reclassified_as_census_count() -> None:
    payload = _payload()
    compiler = _source(payload, "compiler_data")
    compiler["identity"]["external_precursors"][0]["reported_rows_are_sft1_census_counts"] = True
    with pytest.raises(ValidationError):
        Wave1SourceCensus.model_validate(payload)


@pytest.mark.parametrize("status", ["unknown", "missing"])
def test_unavailable_proof_cannot_be_n31_eligible(status: str) -> None:
    payload = _payload()
    proof = _source(payload, "cslib")["n31_source_proof"]
    proof["status"] = status
    proof["n31_n_proof_eligible"] = True
    with pytest.raises(ValidationError, match="unknown/missing source proof"):
        Wave1SourceCensus.model_validate(payload)


def test_unknown_proof_cannot_carry_partial_evidence() -> None:
    payload = _payload()
    proof = _source(payload, "physlib")["n31_source_proof"]
    proof["proof_inventory_sha256"] = "a" * 64
    with pytest.raises(ValidationError, match="unknown/missing source proof"):
        Wave1SourceCensus.model_validate(payload)


def test_reproducible_proof_requires_every_binding() -> None:
    payload = _payload()
    proof = _source(payload, "mathlib")["n31_source_proof"]
    proof["status"] = "available_reproducible"
    proof["n31_n_proof_eligible"] = True
    proof["blocking_reason"] = None
    with pytest.raises(ValidationError, match="requires all exact evidence bindings"):
        Wave1SourceCensus.model_validate(payload)


def test_source_operation_cannot_be_opened_by_boolean_alone() -> None:
    payload = _payload()
    _source(payload, "mathlib")["operation_eligibility"][0]["eligible"] = True
    with pytest.raises(ValidationError, match="cannot make an operation source-eligible"):
        Wave1SourceCensus.model_validate(payload)


def test_boolean_like_strings_are_rejected() -> None:
    payload = _payload()
    _source(payload, "mathlib")["operation_eligibility"][0]["eligible"] = "false"
    with pytest.raises(ValidationError):
        Wave1SourceCensus.model_validate(payload)


def test_compiler_data_cannot_use_imported_constant_route() -> None:
    payload = _payload()
    compiler = _source(payload, "compiler_data")
    compiler["source_kind"] = "imported_constant"
    compiler["closed_expr"]["route"] = "constant_info_type_with_canonical_universe_instantiation"
    compiler["closed_expr"]["input_kind"] = "imported_constant_name"
    with pytest.raises(ValidationError, match="compiler_data must be signature-elaborated"):
        Wave1SourceCensus.model_validate(payload)


def test_library_cannot_use_signature_elaboration_route() -> None:
    payload = _payload()
    cslib = _source(payload, "cslib")
    cslib["source_kind"] = "extracted_signature"
    cslib["closed_expr"]["route"] = (
        "persistent_term_elab_of_signature_and_complete_telescope_without_declaration"
    )
    cslib["closed_expr"]["input_kind"] = "source_faithful_signature_text"
    with pytest.raises(ValidationError, match="library source must use imported constants"):
        Wave1SourceCensus.model_validate(payload)


def test_source_order_and_coverage_are_exact() -> None:
    payload = _payload()
    payload["sources"] = list(reversed(payload["sources"]))
    with pytest.raises(ValidationError, match="canonical projects in canonical order"):
        Wave1SourceCensus.model_validate(payload)


def test_repo_binding_hash_drift_is_rejected_by_schema() -> None:
    payload = _payload()
    _source(payload, "mathlib")["identity"]["repo_bindings"][0]["sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="repo input bindings differ"):
        Wave1SourceCensus.model_validate(payload)


def test_live_repo_file_drift_fails_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    policy_path = (
        REPO_ROOT / "configs/transformations/sft1_value_first_v1/proposed_composition_policy.yaml"
    ).resolve()

    def drift_one_file(path: Path) -> str:
        if path.resolve() == policy_path:
            return "0" * 64
        return real_hash_file(path)

    monkeypatch.setattr(source_census, "hash_file", drift_one_file)
    with pytest.raises(SourceCensusError, match="repo input drift"):
        load_wave1_source_census(REPO_ROOT)


def test_module_has_no_lean_runtime_or_process_dependency() -> None:
    text = Path(source_census.__file__).read_text(encoding="utf-8")
    assert "leanfaith.lean" not in text
    assert "subprocess" not in text
    assert "lake env lean" not in text

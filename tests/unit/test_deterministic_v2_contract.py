"""LF-031: strict design-only deterministic-v2 contract."""

from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from leanfaith.config.hashing import hash_file
from leanfaith.transforms.registry import load_transformation_registry
from leanfaith.transforms.v2_contract import (
    EXPECTED_HOLDOUT_PARTITION,
    EXPECTED_OVERLAP_EXCLUSIONS,
    EXPECTED_OVERLAP_OWNERS,
    EXPECTED_V2_FAMILY_IDS,
    DeterministicV2PortfolioConfig,
    V2ContractError,
    V2EvidenceClass,
    V2GenerationAddendum,
    load_v2_portfolio,
)

_REGISTRY_SHA256 = "5b8941264d1223069d6cb8c49b54e6e719abf3961820cd9d979113164db9d786"
_PROFILE_SHA256 = "9396c50fc088384e4085d95841f1c38ef898f3348580b4be15584e4edc03c2da"
_POLICY_SHA256 = "e41064eb4a6572d0283821ce7b9be21d211d8f149afe00d62e3e241446941cf3"
_EFFECTIVE_V1_HASH = "8a5316dacba064d9b3b13e12dfd46cd707445ecc520101a9374463f336f6466f"


def test_v2_portfolio_is_exact_and_non_executable() -> None:
    loaded = load_v2_portfolio()
    config = loaded.config

    assert tuple(item.family_id for item in config.families) == EXPECTED_V2_FAMILY_IDS
    assert config.runtime_registry_created is False
    assert config.any_family_executable is False
    assert config.draft_emission_authorized is False
    assert config.label_emission_authorized is False
    assert all(item.status == "disabled" for item in config.families)
    assert all(item.implementation_status == "design_only" for item in config.families)
    assert all(item.executable is False for item in config.families)
    assert all(item.draft_emission_authorized is False for item in config.families)
    assert all(item.label_emission_authorized is False for item in config.families)

    evidence = {item.family_id.split("_", 1)[0]: item.evidence_class for item in config.families}
    assert all(evidence[f"p{index:02d}"] == V2EvidenceClass.E0 for index in range(5, 13))
    assert evidence["p13"] == V2EvidenceClass.E1
    assert all(evidence[f"p{index:02d}"] == V2EvidenceClass.E2 for index in range(14, 18))
    assert all(evidence[f"n{index:02d}"] == V2EvidenceClass.D0 for index in range(11, 18))

    assert {
        item.mechanism_id: item.owner_family_id for item in config.overlap_ownership
    } == EXPECTED_OVERLAP_OWNERS
    assert {
        item.mechanism_id: item.excluded_family_ids for item in config.overlap_ownership
    } == EXPECTED_OVERLAP_EXCLUSIONS
    assert {
        item.mechanism_superclass: item.family_ids for item in config.mechanism_holdouts
    } == EXPECTED_HOLDOUT_PARTITION


def test_loading_v2_preserves_v1_bytes_and_effective_replay() -> None:
    before = load_transformation_registry()
    before_dump = before.model_dump(mode="json")
    before_hashes = {
        "registry": hash_file(before.registry_path),
        "profile": hash_file(before.profile_path),
        "policy": hash_file(before.promotion_policy_path),
    }

    load_v2_portfolio()
    after = load_transformation_registry()

    assert before.registry_hash == after.registry_hash == _EFFECTIVE_V1_HASH
    assert before_dump == after.model_dump(mode="json")
    assert before_hashes == {
        "registry": _REGISTRY_SHA256,
        "profile": _PROFILE_SHA256,
        "policy": _POLICY_SHA256,
    }
    assert before_hashes == {
        "registry": hash_file(after.registry_path),
        "profile": hash_file(after.profile_path),
        "policy": hash_file(after.promotion_policy_path),
    }


def _portfolio_payload() -> dict[str, object]:
    return load_v2_portfolio().config.model_dump(mode="python")


@pytest.mark.parametrize(
    "mutation,match",
    [
        (
            lambda value: value["families"][0].__setitem__("evidence_class", "E0"),
            "evidence class D0",
        ),
        (
            lambda value: value["families"][0].__setitem__("executable", True),
            "Input should be False",
        ),
        (
            lambda value: value.__setitem__("families", value["families"][:-1]),
            "exact sorted P05-P17/N11-N17 portfolio",
        ),
        (
            lambda value: value["overlap_ownership"][0].__setitem__(
                "owner_family_id", "p05_resolved_names"
            ),
            "wrong owner",
        ),
        (
            lambda value: value["overlap_ownership"][0].__setitem__(
                "excluded_family_ids", ("p05_resolved_names",)
            ),
            "wrong exclusions",
        ),
        (
            lambda value: value["mechanism_holdouts"][0].__setitem__(
                "family_ids",
                value["mechanism_holdouts"][0]["family_ids"][:-1],
            ),
            "frozen superclass partition",
        ),
    ],
)
def test_v2_contract_rejects_portfolio_drift(mutation: object, match: str) -> None:
    payload = copy.deepcopy(_portfolio_payload())
    mutation(payload)  # type: ignore[operator]
    with pytest.raises((ValidationError, ValueError), match=match):
        DeterministicV2PortfolioConfig.model_validate(payload)


def test_v2_load_fails_closed_when_an_accepted_v1_byte_changes(tmp_path: Path) -> None:
    (tmp_path / "PLAN.md").write_text("fixture\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    for relative in (
        "configs/transformations/registry.yaml",
        "configs/transformations/v1.yaml",
        "configs/transformations/v2.yaml",
        "policies/transformation_promotion_v1.yaml",
    ):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(relative, destination)
    registry = tmp_path / "configs/transformations/registry.yaml"
    registry.write_bytes(registry.read_bytes() + b"\n")

    with pytest.raises(V2ContractError, match="accepted v1 artifact changed"):
        load_v2_portfolio(tmp_path)


def _valid_addendum_payload() -> dict[str, object]:
    loaded = load_v2_portfolio()
    family_ids = tuple(item.family_id for item in loaded.config.families)
    return {
        "schema_version": 1,
        "addendum_id": "future_generation_receipt",
        "portfolio_id": loaded.config.portfolio_id,
        "portfolio_version": loaded.config.portfolio_version,
        "portfolio_config_hash": loaded.config_hash,
        "accepted_v1_effective_registry_hash": _EFFECTIVE_V1_HASH,
        "execution_profile_id": "future_v2_execution_profile",
        "execution_profile_hash": "1" * 64,
        "family_ids": family_ids,
        "evidence_classes": {
            item.family_id: item.evidence_class for item in loaded.config.families
        },
        "source_inventory_hash": "2" * 64,
        "coverage_report_hash": "3" * 64,
        "clean_replay_hash_a": "4" * 64,
        "clean_replay_hash_b": "4" * 64,
        "failure_accounting_hash": "5" * 64,
        "lean_validation_report_hash": "6" * 64,
        "overlap_audit_hash": "7" * 64,
        "split_audit_hash": "8" * 64,
        "denylist_audit_hash": "9" * 64,
        "emitted_draft_count": 123,
        "resolved_label_count": 0,
        "promoted_item_count": 0,
        "grants_generation_credit_only": True,
        "training_eligible": False,
    }


def test_generation_addendum_is_generation_credit_only() -> None:
    addendum = V2GenerationAddendum.model_validate(_valid_addendum_payload())
    assert addendum.emitted_draft_count == 123
    assert addendum.resolved_label_count == 0
    assert addendum.promoted_item_count == 0
    assert addendum.training_eligible is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("resolved_label_count", 1),
        ("promoted_item_count", 1),
        ("training_eligible", True),
        ("clean_replay_hash_b", "a" * 64),
    ],
)
def test_generation_addendum_rejects_promotion_or_dirty_replay(
    field: str,
    value: object,
) -> None:
    payload = _valid_addendum_payload()
    payload[field] = value
    with pytest.raises(ValidationError):
        V2GenerationAddendum.model_validate(payload)

"""Code-owned LF-018 unary-negative construction and registration."""

from __future__ import annotations

import hashlib
from collections.abc import Callable

import pytest

from leanfaith.config.hashing import hash_canonical
from leanfaith.schemas import CANONICAL_VIEW_NAMES, IntendedRelation, ViewStatus
from leanfaith.transforms.negative_factory import (
    NegativeRuleFactoryError,
    build_negative_rule_runtime,
)
from leanfaith.transforms.protocol import PairTransformationRule, TransformationRule
from leanfaith.transforms.registry import (
    LoadedTransformationRegistry,
    RuleImplementationStatus,
    TransformationRegistryConfig,
    load_transformation_registry,
)
from tests.unit.record_factories import representation_record, theorem_record


def _rehash_config(
    mutate: Callable[[dict[str, object]], None],
) -> LoadedTransformationRegistry:
    loaded = load_transformation_registry()
    config_payload = loaded.config.model_dump(mode="python")
    mutate(config_payload)
    config = TransformationRegistryConfig.model_validate(config_payload)
    registry_config_hash = hash_canonical(config.model_dump(mode="json"))
    registry_hash = hash_canonical(
        {
            "schema": "leanfaith_transformation_registry_effective_v1",
            "registry": config.model_dump(mode="json"),
            "profile": loaded.profile.model_dump(mode="json"),
            "promotion_policy_hash": loaded.promotion_policy_hash,
        }
    )
    payload = loaded.model_dump(mode="python")
    payload.update(
        {
            "config": config,
            "registry_config_hash": registry_config_hash,
            "registry_hash": registry_hash,
        }
    )
    return LoadedTransformationRegistry.model_validate(payload)


def _mutate_rule(
    payload: dict[str, object],
    rule_id: str,
    **updates: object,
) -> None:
    families = payload["families"]
    assert isinstance(families, list | tuple)
    for family in families:
        assert isinstance(family, dict)
        rules = family["rules"]
        assert isinstance(rules, list | tuple)
        for rule in rules:
            assert isinstance(rule, dict)
            if rule["rule_id"] == rule_id:
                rule.update(updates)
                return
    raise AssertionError(f"rule {rule_id!r} not found")


def _n01_records():
    code = "theorem n01_fixture (m n : Nat) : m < n := by sorry"
    theorem = theorem_record(
        declaration_name="n01_fixture",
        declaration_full_name="n01_fixture",
        proof_stripped_declaration=code,
        statement_content_hash=hashlib.sha256(code.encode("utf-8")).hexdigest(),
    )
    statuses = dict.fromkeys(CANONICAL_VIEW_NAMES, ViewStatus.NOT_ATTEMPTED)
    for view in (
        "raw_proof_stripped",
        "headless",
        "signature_pp",
        "signature_explicit",
        "semantic_atoms",
        "operator_tree",
    ):
        statuses[view] = ViewStatus.OK
    representation = representation_record(
        raw_proof_stripped=code,
        headless=code,
        signature_pp="m < n",
        signature_explicit="LT.lt m n",
        semantic_atoms=("const:LT.lt", "const:Nat", "const:instLTNat"),
        operator_tree={"kind": "fixture"},
        alpha_identity_fingerprint="a" * 64,
        view_status=statuses,
        content_hash=hash_canonical({"fixture": "negative_factory_n01"}),
    )
    return theorem, representation


def test_repository_available_unary_negative_rules_are_registered_from_static_code() -> None:
    loaded = load_transformation_registry()

    result = build_negative_rule_runtime(loaded)

    assert result.registry_hash == loaded.registry_hash
    assert result.registered_rule_ids == (
        "n01_operator",
        "n02_quantifier",
        "n03_drop_hypothesis",
        "n07_literal_bound",
    )
    assert result.skipped_rule_ids == ()
    assert result.pair_aware_rule_ids == ("n10_nearby_theorem",)
    assert len(result.pair_rules) == 1
    n10 = result.pair_rules[0]
    assert n10.rule_id == "n10_nearby_theorem"
    assert n10.generation_config_hash == loaded.registry_hash
    assert isinstance(n10, PairTransformationRule)
    assert not isinstance(n10, TransformationRule)


def test_registered_rule_uses_effective_hash_and_emits_only_provisional_intention() -> None:
    loaded = load_transformation_registry()
    result = build_negative_rule_runtime(loaded)
    theorem, representation = _n01_records()

    execution = result.runtime.execute("n01_operator", theorem, representation, 3)

    assert execution.attempt.terminal_outcome == "generated"
    assert execution.attempt.generation_config_hash == loaded.registry_hash
    assert len(execution.drafts) == 1
    draft = execution.drafts[0]
    assert draft.generation_config_hash == loaded.registry_hash
    assert draft.intended_relation == IntendedRelation.NEAR_MISS
    assert draft.candidate_pool == "deterministic_negative_provisional"
    assert draft.metadata["semantic_negative_established"] is False


def test_configured_pending_unary_rule_is_skipped_not_constructed() -> None:
    loaded = _rehash_config(
        lambda payload: _mutate_rule(
            payload,
            "n01_operator",
            implementation_status=RuleImplementationStatus.PENDING,
        )
    )

    result = build_negative_rule_runtime(loaded)

    assert "n01_operator" not in result.registered_rule_ids
    assert result.skipped_rule_ids == ("n01_operator",)
    assert result.pair_aware_rule_ids == ("n10_nearby_theorem",)
    assert tuple(rule.rule_id for rule in result.pair_rules) == ("n10_nearby_theorem",)


def test_pair_aware_n10_is_reported_but_never_registered_in_unary_runtime() -> None:
    loaded = _rehash_config(
        lambda payload: _mutate_rule(
            payload,
            "n10_nearby_theorem",
            implementation_status=RuleImplementationStatus.AVAILABLE,
        )
    )

    result = build_negative_rule_runtime(loaded)

    assert result.pair_aware_rule_ids == ("n10_nearby_theorem",)
    assert tuple(rule.rule_id for rule in result.pair_rules) == ("n10_nearby_theorem",)
    assert "n10_nearby_theorem" not in result.registered_rule_ids
    assert "n10_nearby_theorem" not in result.skipped_rule_ids


def test_pending_pair_rule_is_identified_but_not_constructed() -> None:
    loaded = _rehash_config(
        lambda payload: _mutate_rule(
            payload,
            "n10_nearby_theorem",
            implementation_status=RuleImplementationStatus.PENDING,
        )
    )

    result = build_negative_rule_runtime(loaded)

    assert result.pair_aware_rule_ids == ("n10_nearby_theorem",)
    assert result.pair_rules == ()
    assert "n10_nearby_theorem" not in result.registered_rule_ids
    assert "n10_nearby_theorem" not in result.skipped_rule_ids


def test_unknown_available_unary_negative_implementation_key_is_rejected() -> None:
    loaded = _rehash_config(
        lambda payload: _mutate_rule(
            payload,
            "n01_operator",
            implementation_key="yaml_selected_unknown",
        )
    )

    with pytest.raises(
        NegativeRuleFactoryError,
        match=r"non-code-owned.*yaml_selected_unknown",
    ):
        build_negative_rule_runtime(loaded)


def test_n10_key_cannot_reclassify_another_rule_as_pair_aware() -> None:
    loaded = _rehash_config(
        lambda payload: _mutate_rule(
            payload,
            "n01_operator",
            implementation_key="n10_nearby_theorem",
        )
    )

    with pytest.raises(
        NegativeRuleFactoryError,
        match=r"non-code-owned.*n10_nearby_theorem",
    ):
        build_negative_rule_runtime(loaded)


def test_duplicate_available_unary_negative_implementation_key_is_rejected() -> None:
    loaded = _rehash_config(
        lambda payload: _mutate_rule(
            payload,
            "n03_drop_hypothesis",
            implementation_key="n02_quantifier",
        )
    )

    with pytest.raises(
        NegativeRuleFactoryError,
        match=r"implementation_key is configured more than once",
    ):
        build_negative_rule_runtime(loaded)


def test_registry_and_unary_code_metadata_mismatch_is_rejected() -> None:
    loaded = _rehash_config(
        lambda payload: _mutate_rule(
            payload,
            "n01_operator",
            rule_version="1.0.1",
        )
    )

    with pytest.raises(
        NegativeRuleFactoryError,
        match="metadata mismatch: rule_version",
    ):
        build_negative_rule_runtime(loaded)


def test_registry_and_pair_code_metadata_mismatch_is_rejected() -> None:
    loaded = _rehash_config(
        lambda payload: _mutate_rule(
            payload,
            "n10_nearby_theorem",
            rule_version="1.0.1",
        )
    )

    with pytest.raises(
        NegativeRuleFactoryError,
        match="metadata mismatch: rule_version",
    ):
        build_negative_rule_runtime(loaded)

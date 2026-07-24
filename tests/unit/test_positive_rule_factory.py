"""Code-owned LF-017 positive-rule construction and registration."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from leanfaith.config.hashing import hash_canonical
from leanfaith.schemas import CANONICAL_VIEW_NAMES, ViewStatus
from leanfaith.transforms.factory import (
    PositiveRuleFactoryError,
    build_positive_rule_runtime,
)
from leanfaith.transforms.registry import (
    LoadedTransformationRegistry,
    RejectionReason,
    TransformationRegistryConfig,
    TransformationRejected,
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


def _records(code: str, *, operator_tree: bool = False):
    theorem = theorem_record(
        declaration_name="t",
        declaration_full_name="t",
        proof_stripped_declaration=code,
    )
    statuses = dict(representation_record().view_status)
    tree = None
    if operator_tree:
        statuses["operator_tree"] = ViewStatus.OK
        tree = {"kind": "fixture"}
    representation = representation_record(
        raw_proof_stripped=code,
        alpha_identity_fingerprint="a" * 64,
        operator_tree=tree,
        view_status={name: statuses[name] for name in CANONICAL_VIEW_NAMES},
    )
    return theorem, representation


def test_repository_available_positive_rules_are_registered_from_static_code() -> None:
    loaded = load_transformation_registry()

    result = build_positive_rule_runtime(loaded)

    assert result.registry_hash == loaded.registry_hash
    assert result.registered_rule_ids == (
        "p01_alpha",
        "p02_binders",
        "p04_notation_lite",
    )
    assert result.skipped_rule_ids == ("p00_cosmetic",)


def test_registered_p01_and_p02_execute_with_effective_registry_hash() -> None:
    loaded = load_transformation_registry()
    result = build_positive_rule_runtime(loaded)
    p01_theorem, p01_representation = _records("theorem t (x : Nat) : x = x := by sorry")
    p02_theorem, p02_representation = _records(
        "theorem t (x y : Nat) : x = x := by sorry",
        operator_tree=True,
    )

    p01 = result.runtime.execute(
        "p01_alpha",
        p01_theorem,
        p01_representation,
        3,
    )
    p02 = result.runtime.execute(
        "p02_binders",
        p02_theorem,
        p02_representation,
        3,
    )

    assert p01.attempt.terminal_outcome == "generated"
    assert p02.attempt.terminal_outcome == "generated"
    assert p01.drafts[0].generation_config_hash == loaded.registry_hash
    assert p02.drafts[0].generation_config_hash == loaded.registry_hash


def test_configured_pending_rule_is_skipped_not_constructed() -> None:
    loaded = _rehash_config(
        lambda payload: _mutate_rule(
            payload,
            "p01_alpha",
            implementation_status="pending",
        )
    )

    result = build_positive_rule_runtime(loaded)

    assert "p01_alpha" not in result.registered_rule_ids
    assert "p01_alpha" in result.skipped_rule_ids
    theorem, representation = _records("theorem t (x : Nat) : x = x := by sorry")
    with pytest.raises(TransformationRejected) as caught:
        result.runtime.execute("p01_alpha", theorem, representation, 0)
    assert caught.value.event.reason_code == RejectionReason.IMPLEMENTATION_UNAVAILABLE


def test_unknown_available_positive_implementation_key_is_rejected() -> None:
    loaded = _rehash_config(
        lambda payload: _mutate_rule(
            payload,
            "p01_alpha",
            implementation_key="yaml_selected_unknown",
        )
    )

    with pytest.raises(
        PositiveRuleFactoryError,
        match=r"non-code-owned.*yaml_selected_unknown",
    ):
        build_positive_rule_runtime(loaded)


def test_registry_and_code_metadata_mismatch_is_rejected() -> None:
    loaded = _rehash_config(
        lambda payload: _mutate_rule(
            payload,
            "p01_alpha",
            rule_version="1.0.1",
        )
    )

    with pytest.raises(
        PositiveRuleFactoryError,
        match="metadata mismatch: rule_version",
    ):
        build_positive_rule_runtime(loaded)

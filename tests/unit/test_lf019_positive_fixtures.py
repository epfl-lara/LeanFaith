"""Focused contract tests for the LF-019 positive smoke fixtures."""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from leanfaith.schemas import (
    CANONICAL_VIEW_NAMES,
    RepresentationRecord,
    TheoremRecord,
    ValidationStatus,
    ViewStatus,
    make_id,
)
from leanfaith.transforms.factory import build_positive_rule_runtime
from leanfaith.transforms.positive_fixtures import (
    PositiveFixtureProfile,
    load_lf019_positive_fixture_profile,
)
from leanfaith.transforms.registry import load_transformation_registry
from tests.unit.record_factories import UTC_NOW

_EXPECTED_RULE_IDS = (
    "p01_alpha",
    "p02_binders",
    "p04_notation_lite",
)


def _theorem_and_representation(
    *,
    rule_id: str,
    source_name: str,
    source_code: str,
) -> tuple[TheoremRecord, RepresentationRecord]:
    theorem_id = make_id("thm", {"lf019_positive_fixture": rule_id})
    ancestry_id = make_id("anc", {"lf019_positive_fixture": rule_id})
    context_id = "ctx:" + "0" * 64
    theorem = TheoremRecord(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        source="lf019_positive_fixture",
        source_revision="unit",
        source_record=rule_id,
        context_id=context_id,
        declaration_kind="theorem",
        declaration_name=source_name,
        declaration_full_name=source_name,
        proof_stripped_declaration=source_code,
        inline_elaboration_source=source_code,
        is_proposition=True,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        statement_content_hash=hashlib.sha256(source_code.encode("utf-8")).hexdigest(),
    )
    representation = RepresentationRecord(
        representation_id=make_id(
            "repr",
            {"lf019_positive_fixture": rule_id, "normalization": "repr_v2"},
        ),
        theorem_id=theorem_id,
        normalization_version="repr_v2",
        context_id=context_id,
        raw_proof_stripped=source_code,
        headless=": fixture",
        signature_pp="fixture",
        signature_explicit="fixture",
        semantic_atoms=("const:Nat",),
        operator_tree={"kind": "const", "name": "Nat"},
        alpha_identity_fingerprint="a" * 64,
        view_status={
            name: (
                ViewStatus.OK
                if name
                in {
                    "raw_proof_stripped",
                    "headless",
                    "signature_pp",
                    "signature_explicit",
                    "semantic_atoms",
                    "operator_tree",
                }
                else ViewStatus.NOT_ATTEMPTED
            )
            for name in CANONICAL_VIEW_NAMES
        },
        content_hash="b" * 64,
        created_at=UTC_NOW,
    )
    return theorem, representation


def test_checked_in_profile_matches_exact_active_positive_inventory() -> None:
    fixtures = load_lf019_positive_fixture_profile()
    registry = load_transformation_registry()
    registration = build_positive_rule_runtime(registry)

    assert fixtures.config.rule_ids == _EXPECTED_RULE_IDS
    assert fixtures.active_rule_ids == registration.registered_rule_ids
    assert fixtures.registry_hash == registry.registry_hash
    assert fixtures.config.artifact_class == "smoke"
    assert fixtures.config.release_eligible is False
    assert fixtures.config.model_selection_eligible is False
    assert fixtures.config.resolution_policy == "provisional_only"


def test_every_checked_in_positive_fixture_is_applicable_and_generates() -> None:
    fixtures = load_lf019_positive_fixture_profile()
    registration = build_positive_rule_runtime(load_transformation_registry())

    for case in fixtures.config.cases:
        theorem, representation = _theorem_and_representation(
            rule_id=case.rule_id,
            source_name=case.source_name,
            source_code=case.source_code,
        )
        execution = registration.runtime.execute(
            case.rule_id,
            theorem,
            representation,
            case.seed,
        )

        assert execution.attempt.applicability is not None
        assert execution.attempt.applicability.applicable is True
        assert execution.attempt.terminal_outcome == "generated"
        assert len(execution.drafts) == 1
        draft = execution.drafts[0]
        assert case.expected_candidate_fragment in draft.candidate_code
        assert draft.transformation_trace[0]["operation"] == (case.expected_trace_operation)


def test_profile_rejects_missing_or_duplicate_positive_family() -> None:
    loaded = load_lf019_positive_fixture_profile()
    payload = loaded.config.model_dump(mode="python")
    cases = payload["cases"]
    assert isinstance(cases, list | tuple)
    payload["cases"] = [cases[0], cases[0], cases[2]]

    with pytest.raises(
        ValidationError,
        match="cases must contain exactly one fixture for each scoped positive rule",
    ):
        PositiveFixtureProfile.model_validate(payload)


def test_profile_rejects_rule_specific_trace_contract_drift() -> None:
    loaded = load_lf019_positive_fixture_profile()
    payload = loaded.config.model_dump(mode="python")
    cases = payload["cases"]
    assert isinstance(cases, list | tuple)
    first = dict(cases[0])
    first["expected_trace_operation"] = "replace_exact_span"
    payload["cases"] = [first, *cases[1:]]

    with pytest.raises(
        ValidationError,
        match="p01_alpha expected_trace_operation must be 'alpha_rename'",
    ):
        PositiveFixtureProfile.model_validate(payload)

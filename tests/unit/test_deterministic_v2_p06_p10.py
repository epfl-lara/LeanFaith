"""Focused conservative-scope tests for experimental P06/P10 E0 rules."""

from __future__ import annotations

import hashlib

import pytest

from leanfaith.representations import alpha_identity_fingerprint
from leanfaith.schemas import CANONICAL_VIEW_NAMES, ViewStatus, make_id
from leanfaith.schemas.enums import IntendedRelation, QualityTier, ValidationStatus
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms.positives.p06_p10_surface import (
    P06ImplicitArgumentsRule,
    P10ConstructorsRule,
    enumerate_p06_sites,
    enumerate_p10_sites,
)
from leanfaith.transforms.positives.v2_e0 import apply_presentation_trace
from tests.unit.record_factories import representation_record, theorem_record


def _tree() -> dict[str, object]:
    return {
        "root": {
            "k": "forall",
            "bi": "default",
            "dom": {"k": "sort", "u": "u.1"},
            "body": {"k": "const", "n": "Prop", "us": ["u.1"]},
        }
    }


def _records(source: str, key: str) -> tuple[TheoremRecord, RepresentationRecord]:
    tree = _tree()
    theorem_id = make_id("thm", {"p06_p10": key})
    ancestry_id = make_id("anc", {"p06_p10": key})
    theorem = theorem_record(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        declaration_name=key,
        declaration_full_name=key,
        proof_stripped_declaration=source,
        inline_elaboration_source="import LeanFaithFixtures\n" + source,
        statement_content_hash=hashlib.sha256(source.encode()).hexdigest(),
    )
    statuses = {
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
    }
    representation = representation_record(
        representation_id=make_id("repr", {"p06_p10": key}),
        theorem_id=theorem_id,
        raw_proof_stripped=source,
        headless=source.split(":", 1)[-1].split(":=", 1)[0],
        signature_pp="fixture type",
        signature_explicit="fixture explicit type",
        semantic_atoms=("const:Prop",),
        operator_tree=tree,
        alpha_identity_fingerprint=alpha_identity_fingerprint(tree),
        view_status=statuses,
    )
    return theorem, representation


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "theorem p (xs : List Nat) : @List.length Nat xs = xs.length := by sorry",
            "theorem p (xs : List Nat) : List.length xs = xs.length := by sorry",
        ),
        (
            "theorem p (xs ys : List Nat) : @List.append Nat xs ys = xs ++ ys := by sorry",
            "theorem p (xs ys : List Nat) : List.append xs ys = xs ++ ys := by sorry",
        ),
        (
            "theorem p (p : Nat × Bool) : @Prod.fst Nat Bool p = p.1 := by sorry",
            "theorem p (p : Nat × Bool) : Prod.fst p = p.1 := by sorry",
        ),
    ],
)
def test_p06_exact_single_span_and_inverse(source: str, expected: str) -> None:
    rule = P06ImplicitArgumentsRule(generation_config_hash="a" * 64, candidate_pool="fixture")
    theorem, representation = _records(source, hashlib.sha256(source.encode()).hexdigest())
    draft = rule.generate(theorem, representation, seed=7)[0]
    assert draft.candidate_code == expected
    assert draft.inverse_trace is not None
    assert apply_presentation_trace(draft.candidate_code, draft.inverse_trace) == source
    assert len(draft.transformation_trace) == 1
    assert draft.intended_relation == IntendedRelation.EQUIVALENT
    assert draft.metadata == {"generation_intention_only": True}


@pytest.mark.parametrize(
    "source",
    [
        # Instance-bearing/unregistered head.
        "theorem p (xs : List Nat) (f : Nat → Bool) : @List.filter Nat inst f xs = xs := by sorry",
        # Nested application argument.
        "theorem p (xs : List Nat) : @List.length Nat (List.reverse xs) = 0 := by sorry",
        # The matched application is itself a nested argument.
        "theorem p (xs : List Nat) (f : Nat → Nat) : f (@List.length Nat xs) = 0 := by sorry",
        # Metavariable placeholder.
        "theorem p (xs : List Nat) : @List.length _ xs = 0 := by sorry",
        # Unknown head cannot assert ordinary versus instance implicits.
        "theorem p (xs : List Nat) : @Mystery.f Nat xs = 0 := by sorry",
    ],
)
def test_p06_fails_closed_outside_allowlisted_atomic_root_spines(source: str) -> None:
    assert enumerate_p06_sites(source) == ()


@pytest.mark.parametrize(
    ("source", "operation", "expected_fragment"),
    [
        (
            "theorem p (x : Nat) (y : Bool) : (⟨x, y⟩ : Nat × Bool) = (x, y) := by sorry",
            "anonymous_constructor_to_tuple",
            "((x, y) : Nat × Bool)",
        ),
        (
            "theorem p (x : Nat) (y : Bool) : ((x, y) : Nat × Bool) = ⟨x, y⟩ := by sorry",
            "tuple_to_anonymous_constructor",
            "(⟨x, y⟩ : Nat × Bool)",
        ),
    ],
)
def test_p10_exact_ascribed_prod_edit_and_inverse(
    source: str,
    operation: str,
    expected_fragment: str,
) -> None:
    sites = enumerate_p10_sites(source)
    assert sites[0].operation == operation
    rule = P10ConstructorsRule(generation_config_hash="b" * 64, candidate_pool="fixture")
    theorem, representation = _records(source, operation)
    draft = rule.generate(theorem, representation, seed=0)[0]
    assert expected_fragment in draft.candidate_code
    assert draft.inverse_trace is not None
    assert apply_presentation_trace(draft.candidate_code, draft.inverse_trace) == source
    assert len(draft.transformation_trace) == 1


@pytest.mark.parametrize(
    "source",
    [
        # Ambiguous expected type.
        "theorem p (x : Nat) (y : Bool) : ⟨x, y⟩ = ⟨x, y⟩ := by sorry",
        # Dependent/subtype constructor, not Prod.
        "theorem p (x : Nat) (h : x = x) : (⟨x, h⟩ : {n // n = n}).1 = x := by sorry",
        # More than two constructor fields.
        "theorem p (x y z : Nat) : (⟨x, y, z⟩ : Nat × Nat × Nat) = (x, y, z) := by sorry",
        # Nested constructor argument.
        "theorem p (x y z : Nat) : (⟨⟨x, y⟩, z⟩ : (Nat × Nat) × Nat) = ((x, y), z) := by sorry",
    ],
)
def test_p10_fails_closed_without_simple_explicit_prod_ascription(source: str) -> None:
    assert enumerate_p10_sites(source) == ()


def test_clean_mechanical_audit_remains_provisional_and_nontraining() -> None:
    source = "theorem p (xs : List Nat) : @List.length Nat xs = xs.length := by sorry"
    rule = P06ImplicitArgumentsRule(generation_config_hash="c" * 64, candidate_pool="fixture")
    theorem, representation = _records(source, "audit")
    draft = rule.generate(theorem, representation, seed=1)[0]
    candidate = theorem.model_copy(
        update={
            "theorem_id": make_id("thm", {"candidate": draft.draft_id}),
            "proof_stripped_declaration": draft.candidate_code,
            "statement_content_hash": hashlib.sha256(draft.candidate_code.encode()).hexdigest(),
        }
    )
    candidate_representation = representation.model_copy(
        update={
            "representation_id": make_id("repr", {"candidate": draft.draft_id}),
            "theorem_id": candidate.theorem_id,
            "raw_proof_stripped": draft.candidate_code,
        }
    )
    audit = rule.audit(theorem, representation, candidate, candidate_representation, draft)
    assert audit.violation_codes == ()
    assert audit.recommended_validation_status == ValidationStatus.ELABORATES_WITH_PLACEHOLDER
    assert audit.recommended_quality_tier == QualityTier.PROVISIONAL
    assert audit.metadata["resolved_semantic_label"] is False
    assert audit.metadata["training_eligible"] is False

"""Focused LF-033 tests for the provisional P15 E2 family."""

from __future__ import annotations

import hashlib

import pytest

from leanfaith.representations import alpha_identity_fingerprint
from leanfaith.representations.atoms import operator_tree, semantic_atoms
from leanfaith.schemas import CANONICAL_VIEW_NAMES, RepresentationRecord, TheoremRecord
from leanfaith.schemas.enums import IntendedRelation, QualityTier, ValidationStatus, ViewStatus
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.variant import VariantDraft
from leanfaith.transforms.materialize import build_derived_theorem_record
from leanfaith.transforms.positives.p15_iff_reversal import (
    P15IffReversalError,
    P15IffReversalRule,
    apply_p15_trace,
    build_iff_reversal_root,
    certify_iff_reversal,
    enumerate_p15_sites,
)
from leanfaith.transforms.v2_e2_p15_runtime import build_v2_e2_p15_runtime
from tests.unit.record_factories import representation_record, theorem_record

_SOURCE = "theorem p15 (P Q : Prop) : P ↔ Q := by sorry"
_CANDIDATE = "theorem p15 (P Q : Prop) : Q ↔ P := by sorry"


def _prop() -> dict[str, object]:
    return {"k": "sort", "u": "0"}


def _app(function: dict[str, object], argument: dict[str, object]) -> dict[str, object]:
    return {"k": "app", "fn": function, "arg": argument}


def _iff(*, swapped: bool = False, identical: bool = False) -> dict[str, object]:
    left_index = 0 if swapped else 1
    right_index = 1 if swapped else 0
    if identical:
        right_index = left_index
    node: dict[str, object] = {"k": "const", "n": "Iff", "us": "[]"}
    return _app(
        _app(node, {"k": "bvar", "i": left_index}),
        {"k": "bvar", "i": right_index},
    )


def _root(*, swapped: bool = False, identical: bool = False) -> dict[str, object]:
    return {
        "k": "forall",
        "bi": "default",
        "dom": _prop(),
        "body": {
            "k": "forall",
            "bi": "default",
            "dom": _prop(),
            "body": _iff(swapped=swapped, identical=identical),
        },
    }


def _records(
    source: str,
    key: str,
    root: dict[str, object],
) -> tuple[TheoremRecord, RepresentationRecord]:
    theorem_id = make_id("thm", {"p15": key})
    ancestry_id = make_id("anc", {"p15": key})
    theorem = theorem_record(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        declaration_name="p15",
        declaration_full_name="p15",
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
        representation_id=make_id("repr", {"p15": key}),
        theorem_id=theorem_id,
        raw_proof_stripped=source,
        headless="(P Q : Prop) : P ↔ Q",
        signature_pp="(P Q : Prop) : P ↔ Q",
        signature_explicit="∀ (P Q : Prop), Iff P Q",
        semantic_atoms=semantic_atoms(root),
        operator_tree=operator_tree(root),
        alpha_identity_fingerprint=alpha_identity_fingerprint(root),
        view_status=statuses,
    )
    return theorem, representation


def _candidate_records(
    source: TheoremRecord,
    source_representation: RepresentationRecord,
    draft: VariantDraft,
) -> tuple[TheoremRecord, RepresentationRecord]:
    candidate = build_derived_theorem_record(
        draft=draft,
        sources=(source,),
        primary_source_id=source.theorem_id,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        inline_elaboration_source="import LeanFaithFixtures\n" + draft.candidate_code,
    )
    candidate_root = _root(swapped=True)
    candidate_representation = source_representation.model_copy(
        update={
            "representation_id": make_id("repr", {"p15_candidate": candidate.theorem_id}),
            "theorem_id": candidate.theorem_id,
            "raw_proof_stripped": draft.candidate_code,
            "headless": "(P Q : Prop) : Q ↔ P",
            "signature_pp": "(P Q : Prop) : Q ↔ P",
            "signature_explicit": "∀ (P Q : Prop), Iff Q P",
            "semantic_atoms": semantic_atoms(candidate_root),
            "operator_tree": operator_tree(candidate_root),
            "alpha_identity_fingerprint": alpha_identity_fingerprint(candidate_root),
        }
    )
    return candidate, candidate_representation


def test_p15_enumerates_and_builds_exact_root_iff_reversal() -> None:
    (site,) = enumerate_p15_sites(_SOURCE, operator_tree(_root()))
    assert (site.left_text, site.right_text) == ("P", "Q")
    assert site.header_binder_count == 2
    assert build_iff_reversal_root(_root(), 2) == _root(swapped=True)


def test_p15_trace_round_trip_preserves_complete_complex_sides() -> None:
    source = "theorem p15 (P Q R : Prop) : (P ∧ Q) ↔ (R → Q) := by sorry"
    complex_root = {
        "k": "forall",
        "bi": "default",
        "dom": _prop(),
        "body": {
            "k": "forall",
            "bi": "default",
            "dom": _prop(),
            "body": {
                "k": "forall",
                "bi": "default",
                "dom": _prop(),
                "body": _iff(),
            },
        },
    }
    (site,) = enumerate_p15_sites(source, operator_tree(complex_root))
    forward = (
        {
            "operation": "swap_exact_spans",
            "left_start": site.left_start,
            "left_end": site.left_end,
            "left_text": site.left_text,
            "right_start": site.right_start,
            "right_end": site.right_end,
            "right_text": site.right_text,
        },
    )
    assert apply_p15_trace(source, forward) == (
        "theorem p15 (P Q R : Prop) : (R → Q) ↔ (P ∧ Q) := by sorry"
    )


def test_p15_rejects_nested_multiple_and_identical_iff() -> None:
    nested = "theorem p15 (P Q : Prop) : True → (P ↔ Q) := by sorry"
    multiple = "theorem p15 (P Q R : Prop) : P ↔ Q ↔ R := by sorry"
    identical = "theorem p15 (P : Prop) : P ↔ P := by sorry"
    assert enumerate_p15_sites(nested, operator_tree(_root())) == ()
    assert enumerate_p15_sites(multiple, operator_tree(_root())) == ()
    assert enumerate_p15_sites(identical, operator_tree(_root(identical=True))) == ()


def test_p15_rejects_iff_below_a_conclusion_quantifier() -> None:
    source = "theorem p15 (P Q : Prop) : ∀ x : Nat, P ↔ Q := by sorry"
    quantified_root = {
        "k": "forall",
        "bi": "default",
        "dom": _prop(),
        "body": {
            "k": "forall",
            "bi": "default",
            "dom": _prop(),
            "body": {
                "k": "forall",
                "bi": "default",
                "dom": {"k": "const", "n": "Nat", "us": "[]"},
                "body": _iff(),
            },
        },
    }
    assert enumerate_p15_sites(source, operator_tree(quantified_root)) == ()


@pytest.mark.parametrize(
    "source",
    [
        "theorem p15 (P Q : Prop) : (∀ x : Nat, P) ↔ Q := by sorry",
        "theorem p15 (P Q : Prop) : P ↔ (∃ x : Nat, Q) := by sorry",
    ],
)
def test_p15_allows_parenthesized_quantifier_sides(source: str) -> None:
    assert len(enumerate_p15_sites(source, operator_tree(_root()))) == 1


@pytest.mark.parametrize(
    "source",
    [
        "theorem p15 (P Q : Prop) : ∀ x : Nat, P ↔ Q := by sorry",
        "theorem p15 (P Q : Prop) : P ↔ ∃ x : Nat, Q := by sorry",
        "theorem p15 (P Q : Prop) : P ∧ ∀ x : Nat, Q ↔ Q := by sorry",
        "theorem p15 (P Q : Prop) : P = fun _ => Q ↔ Q := by sorry",
    ],
)
def test_p15_rejects_unparenthesized_scope_changing_sides(source: str) -> None:
    assert enumerate_p15_sites(source, operator_tree(_root())) == ()


def test_p15_generation_is_invertible_and_provisional() -> None:
    theorem, representation = _records(_SOURCE, "generate", _root())
    rule = P15IffReversalRule(generation_config_hash="d" * 64, candidate_pool="fixture")
    (draft,) = rule.generate(theorem, representation, seed=15)
    assert draft.candidate_code == _CANDIDATE
    assert draft.intended_relation == IntendedRelation.EQUIVALENT
    assert draft.intended_error_types == ()
    assert draft.inverse_trace is not None
    assert apply_p15_trace(draft.candidate_code, draft.inverse_trace) == _SOURCE
    assert draft.metadata["resolved_semantic_label"] is False
    assert draft.metadata["training_eligible"] is False


def test_p15_runtime_binds_the_frozen_e2_portfolio_identity() -> None:
    theorem, representation = _records(_SOURCE, "runtime", _root())
    runtime = build_v2_e2_p15_runtime()
    assert runtime.rule_ids == ("p15_root_iff_reversal",)
    execution = runtime.execute(
        "p15_root_iff_reversal",
        theorem,
        representation,
        seed=15,
    )
    assert execution.attempt.terminal_outcome == "generated"
    assert execution.attempt.family_id == "p15_root_iff_reversal"
    assert execution.attempt.registry_hash == runtime.portfolio_hash
    assert len(execution.drafts) == 1
    assert execution.drafts[0].candidate_code == _CANDIDATE


def test_p15_certificate_accepts_only_exact_root_reversal() -> None:
    certificate = certify_iff_reversal(_root(), _root(swapped=True), 2)
    assert certificate.header_binder_count == 2
    with pytest.raises(P15IffReversalError, match="candidate_not_exact"):
        certify_iff_reversal(_root(), _root(), 2)


def test_p15_clean_audit_remains_e2_provisional() -> None:
    theorem, representation = _records(_SOURCE, "audit", _root())
    rule = P15IffReversalRule(generation_config_hash="e" * 64, candidate_pool="fixture")
    (draft,) = rule.generate(theorem, representation, seed=15)
    candidate, candidate_representation = _candidate_records(theorem, representation, draft)
    audit = rule.audit(
        theorem,
        representation,
        candidate,
        candidate_representation,
        draft,
    )
    assert audit.violation_codes == ()
    assert audit.structural_diff_ok is True
    assert audit.inverse_or_roundtrip_ok is True
    assert audit.recommended_quality_tier == QualityTier.PROVISIONAL
    assert audit.metadata["evidence_class"] == "E2"
    assert audit.metadata["resolved_semantic_label"] is False
    assert audit.metadata["training_eligible"] is False


def test_p15_tampered_candidate_is_quarantined() -> None:
    theorem, representation = _records(_SOURCE, "tamper", _root())
    rule = P15IffReversalRule(generation_config_hash="f" * 64, candidate_pool="fixture")
    (draft,) = rule.generate(theorem, representation, seed=15)
    candidate, candidate_representation = _candidate_records(theorem, representation, draft)
    audit = rule.audit(
        theorem,
        representation,
        candidate,
        candidate_representation.model_copy(
            update={
                "operator_tree": operator_tree(_root()),
                "alpha_identity_fingerprint": alpha_identity_fingerprint(_root()),
            }
        ),
        draft,
    )
    assert "candidate_not_exact_root_iff_reversal" in audit.violation_codes
    assert audit.recommended_quality_tier == QualityTier.UNKNOWN

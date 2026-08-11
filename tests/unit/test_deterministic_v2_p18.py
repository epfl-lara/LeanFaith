"""Focused fail-closed tests for P18 v1.0 root equality symmetry."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast

import pytest

from leanfaith.config.hashing import hash_file
from leanfaith.representations import alpha_identity_fingerprint
from leanfaith.representations.atoms import operator_tree, semantic_atoms
from leanfaith.schemas import CANONICAL_VIEW_NAMES, RepresentationRecord, TheoremRecord
from leanfaith.schemas.enums import IntendedRelation, QualityTier, ValidationStatus, ViewStatus
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.variant import VariantDraft
from leanfaith.transforms.materialize import build_derived_theorem_record
from leanfaith.transforms.positives.p18_equality_symmetry import (
    P18EqualitySymmetryError,
    P18EqualitySymmetryRule,
    apply_p18_trace,
    build_equality_symmetry_root,
    certify_equality_symmetry,
    enumerate_p18_sites,
)
from leanfaith.transforms.protocol import expected_variant_draft_id
from leanfaith.transforms.v2_e2_p18_runtime import build_v2_e2_p18_runtime
from leanfaith.transforms.v2_e2_runtime import build_v2_e2_runtime
from tests.unit.record_factories import representation_record, theorem_record

_SOURCE = "theorem p18 (x y : Nat) : x = y := by sorry"
_CANDIDATE = "theorem p18 (x y : Nat) : y = x := by sorry"


def _nat() -> dict[str, object]:
    return {"k": "const", "n": "Nat", "us": "[]"}


def _app(function: dict[str, object], argument: dict[str, object]) -> dict[str, object]:
    return {"k": "app", "fn": function, "arg": argument}


def _eq(*, swapped: bool = False, identical: bool = False) -> dict[str, object]:
    left_index = 0 if swapped else 1
    right_index = 1 if swapped else 0
    if identical:
        right_index = left_index
    head: dict[str, object] = {"k": "const", "n": "Eq", "us": "[0]"}
    return _app(
        _app(_app(head, _nat()), {"k": "bvar", "i": left_index}),
        {"k": "bvar", "i": right_index},
    )


def _root(*, swapped: bool = False, identical: bool = False) -> dict[str, object]:
    return {
        "k": "forall",
        "bi": "default",
        "dom": _nat(),
        "body": {
            "k": "forall",
            "bi": "default",
            "dom": _nat(),
            "body": _eq(swapped=swapped, identical=identical),
        },
    }


def _records(
    source: str,
    key: str,
    root: dict[str, object],
) -> tuple[TheoremRecord, RepresentationRecord]:
    theorem_id = make_id("thm", {"p18": key})
    ancestry_id = make_id("anc", {"p18": key})
    theorem = theorem_record(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        declaration_name="p18",
        declaration_full_name="p18",
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
        representation_id=make_id("repr", {"p18": key}),
        theorem_id=theorem_id,
        raw_proof_stripped=source,
        headless="(x y : Nat) : x = y",
        signature_pp="(x y : Nat) : x = y",
        signature_explicit="∀ (x y : Nat), Eq x y",
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
            "representation_id": make_id("repr", {"p18_candidate": candidate.theorem_id}),
            "theorem_id": candidate.theorem_id,
            "raw_proof_stripped": draft.candidate_code,
            "headless": "(x y : Nat) : y = x",
            "signature_pp": "(x y : Nat) : y = x",
            "signature_explicit": "∀ (x y : Nat), Eq y x",
            "semantic_atoms": semantic_atoms(candidate_root),
            "operator_tree": operator_tree(candidate_root),
            "alpha_identity_fingerprint": alpha_identity_fingerprint(candidate_root),
        }
    )
    return candidate, candidate_representation


def test_p18_enumerates_exact_root_equality_and_builds_swapped_tree() -> None:
    (site,) = enumerate_p18_sites(_SOURCE, operator_tree(_root()))
    assert (site.left_text, site.right_text) == ("x", "y")
    assert site.header_binder_count == 2
    assert build_equality_symmetry_root(_root(), 2) == _root(swapped=True)


def test_p18_parenthesized_complete_span_round_trip() -> None:
    source = "theorem p18 (x y : Nat) : ((x + 1) = (Nat.succ y)) := by sorry"
    (site,) = enumerate_p18_sites(source, operator_tree(_root()))
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
    candidate = apply_p18_trace(source, forward)
    assert candidate == ("theorem p18 (x y : Nat) : ((Nat.succ y) = (x + 1)) := by sorry")


@pytest.mark.parametrize(
    "source",
    [
        "theorem p18 (x y : Nat) : x = x := by sorry",
        "theorem p18 (x y : Nat) : True → x = y := by sorry",
        "theorem p18 (x y : Nat) : x == y := by sorry",
        "theorem p18 (x y : Nat) : x = by exact y := by sorry",
        "theorem p18 (x y : Nat) : x = (match y with | z => z) := by sorry",
        'theorem p18 (x y : Nat) : x = "unsafe" := by sorry',
        "theorem p18 (x y : Nat) : x = y -- unsafe\n:= by sorry",
        "theorem p18 (x y : Nat) : x = term% y := by sorry",
        "theorem p18 (x y : Nat) : x = $(y) := by sorry",
    ],
)
def test_p18_rejects_nonroot_ambiguous_or_scoped_surfaces(source: str) -> None:
    assert enumerate_p18_sites(source, operator_tree(_root())) == ()


def test_p18_rejects_parser_tree_mismatch_and_identical_tree_sides() -> None:
    iff_root = _root()
    cursor = cast(dict[str, object], cast(dict[str, object], iff_root["body"])["body"])
    cursor["fn"] = {"k": "const", "n": "Iff", "us": "[]"}
    assert enumerate_p18_sites(_SOURCE, operator_tree(iff_root)) == ()
    assert enumerate_p18_sites(_SOURCE, operator_tree(_root(identical=True))) == ()


def test_p18_generation_is_deterministic_invertible_and_provisional() -> None:
    theorem, representation = _records(_SOURCE, "generate", _root())
    rule = P18EqualitySymmetryRule(generation_config_hash="a" * 64, candidate_pool="fixture")
    (draft,) = rule.generate(theorem, representation, seed=18)
    assert draft.candidate_code == _CANDIDATE
    assert draft.intended_relation == IntendedRelation.EQUIVALENT
    assert draft.intended_error_types == ()
    assert draft.inverse_trace is not None
    assert apply_p18_trace(draft.candidate_code, draft.inverse_trace) == _SOURCE
    assert draft.metadata["resolved_semantic_label"] is False
    assert draft.metadata["training_eligible"] is False


def test_p18_runtime_is_versioned_and_binds_immutable_addendum() -> None:
    theorem, representation = _records(_SOURCE, "runtime", _root())
    runtime = build_v2_e2_p18_runtime()
    assert runtime.rule_ids == ("p18_root_equality_symmetry",)
    execution = runtime.execute("p18_root_equality_symmetry", theorem, representation, seed=18)
    assert execution.attempt.terminal_outcome == "generated"
    assert len(execution.drafts) == 1
    dispatched = build_v2_e2_runtime(
        path=Path("configs/transformations/v2_e2_p18_experimental.yaml")
    )
    assert dispatched.rule_ids == ("p18_root_equality_symmetry",)


def test_p18_old_profiles_and_portfolio_bytes_remain_unchanged() -> None:
    expected = {
        "configs/transformations/v2.yaml": (
            "d8d71c0a77bd7afb3f365e40d9ca3ad8c2d989e1d16cab4c4d462da9c14ac487"
        ),
        "configs/transformations/v2_e2_p14_experimental.yaml": (
            "eecbd57d4d593285a1a183f1b271bcf31fa638547fd817bb306ae3a95a6b1587"
        ),
        "configs/transformations/v2_e2_p15_experimental.yaml": (
            "537082b352688991931db40e6edbf7d2351a27b33a87c130bb6acfd3e25a5359"
        ),
        "configs/transformations/v2_e2_p16_experimental.yaml": (
            "98bd56629b99ae3f3cc217c09ce47f6acd0e6dda2d32308649c5a63cc5614211"
        ),
        "configs/transformations/v2_e2_p17_experimental.yaml": (
            "bbf6fc364f1807e852ee6041e7fbc46919e1d6d9e5f3668bc86c139758867563"
        ),
    }
    assert {path: hash_file(Path(path)) for path in expected} == expected


def test_p18_certificate_and_full_representation_audit() -> None:
    certificate = certify_equality_symmetry(_root(), _root(swapped=True), 2)
    assert certificate.header_binder_count == 2
    with pytest.raises(P18EqualitySymmetryError, match="candidate_not_exact"):
        certify_equality_symmetry(_root(), _root(), 2)

    theorem, representation = _records(_SOURCE, "audit", _root())
    rule = P18EqualitySymmetryRule(generation_config_hash="b" * 64, candidate_pool="fixture")
    (draft,) = rule.generate(theorem, representation, seed=18)
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
    assert audit.atom_mapping_ok is True
    assert audit.inverse_or_roundtrip_ok is True
    assert audit.recommended_quality_tier == QualityTier.PROVISIONAL
    assert audit.metadata["resolved_semantic_label"] is False
    assert audit.metadata["training_eligible"] is False


def test_p18_tampered_semantic_view_is_quarantined() -> None:
    theorem, representation = _records(_SOURCE, "tamper", _root())
    rule = P18EqualitySymmetryRule(generation_config_hash="c" * 64, candidate_pool="fixture")
    (draft,) = rule.generate(theorem, representation, seed=18)
    candidate, candidate_representation = _candidate_records(theorem, representation, draft)
    audit = rule.audit(
        theorem,
        representation,
        candidate,
        candidate_representation.model_copy(update={"semantic_atoms": ("tampered",)}),
        draft,
    )
    assert "candidate_alpha_or_semantic_audit_mismatch" in audit.violation_codes
    assert audit.recommended_quality_tier == QualityTier.UNKNOWN


@pytest.mark.parametrize(
    ("updates", "expected_violation"),
    [
        (
            {"intended_relation": IntendedRelation.A_STRONGER},
            "draft_semantic_intention_mismatch",
        ),
        (
            {"intended_error_types": ("E01",)},
            "draft_semantic_intention_mismatch",
        ),
        (
            {"candidate_pool": "tampered_pool"},
            "draft_candidate_pool_mismatch",
        ),
        (
            {
                "metadata": {
                    "positive_intention_only": False,
                    "resolved_semantic_label": False,
                    "structural_direction": "swap_root_equality_sides",
                    "training_eligible": False,
                }
            },
            "draft_fixed_metadata_mismatch",
        ),
        (
            {
                "metadata": {
                    "positive_intention_only": True,
                    "resolved_semantic_label": False,
                    "structural_direction": "different_operation",
                    "training_eligible": False,
                }
            },
            "draft_fixed_metadata_mismatch",
        ),
        (
            {"expected_atom_mapping": {"lhs": "rhs"}},
            "draft_expected_atom_mapping_mismatch",
        ),
        (
            {
                "formalrx_sci_requested": "S1.1",
                "formalrx_sci_validated": "S1.1",
                "formalrx_sci_validation_status": "validated",
                "formalrx_sci_proposer_family": "proposer",
                "formalrx_sci_validator_family": "validator",
            },
            "draft_sci_provenance_mismatch",
        ),
    ],
)
def test_p18_audit_rejects_self_consistently_reidentified_semantic_tampering(
    updates: dict[str, object],
    expected_violation: str,
) -> None:
    theorem, representation = _records(_SOURCE, f"draft-tamper:{expected_violation}", _root())
    rule = P18EqualitySymmetryRule(generation_config_hash="e" * 64, candidate_pool="fixture")
    (draft,) = rule.generate(theorem, representation, seed=18)
    tampered = draft.model_copy(update=updates)
    tampered = tampered.model_copy(update={"draft_id": expected_variant_draft_id(tampered)})
    candidate, candidate_representation = _candidate_records(theorem, representation, tampered)
    audit = rule.audit(
        theorem,
        representation,
        candidate,
        candidate_representation,
        tampered,
    )
    assert expected_violation in audit.violation_codes
    assert audit.recommended_quality_tier == QualityTier.UNKNOWN


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("x", "y"),
        ("x + 1", "Nat.succ y"),
        ("f x", "g y"),
        ("(x, y)", "(y, x)"),
        ("xs.reverse", "ys.map f"),
    ],
)
def test_p18_complete_span_swap_property(left: str, right: str) -> None:
    source = f"theorem p18 (x y : Nat) : ({left}) = ({right}) := by sorry"
    (site,) = enumerate_p18_sites(source, operator_tree(_root()))
    rule = P18EqualitySymmetryRule(generation_config_hash="d" * 64, candidate_pool="fixture")
    theorem, representation = _records(source, f"property:{left}:{right}", _root())
    (draft,) = rule.generate(theorem, representation, seed=18)
    assert draft.inverse_trace is not None
    assert apply_p18_trace(draft.candidate_code, draft.inverse_trace) == source
    assert site.left_text == f"({left})"
    assert site.right_text == f"({right})"

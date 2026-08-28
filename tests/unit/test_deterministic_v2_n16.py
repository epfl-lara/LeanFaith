"""Focused LF-034 tests for the provisional N16 D0 family."""

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
from leanfaith.transforms.negatives.n16_domain_guard import (
    N16DomainGuardError,
    N16DomainGuardRule,
    apply_n16_trace,
    build_domain_guard_removal_root,
    certify_domain_guard_removal,
    enumerate_n16_sites,
)
from leanfaith.transforms.v2_d0_n16_runtime import (
    V2D0N16ExecutionError,
    build_v2_d0_n16_runtime,
    load_v2_d0_n16_execution_config,
)
from tests.unit.record_factories import representation_record, theorem_record

_SOURCE = "theorem n16 (s : List Nat) : ∀ x : Nat, x ∈ s → x = 0 := by sorry"
_CANDIDATE = "theorem n16 (s : List Nat) : ∀ x : Nat, x = 0 := by sorry"


def _nat() -> dict[str, object]:
    return {"k": "const", "n": "Nat", "us": "[]"}


def _list_nat() -> dict[str, object]:
    return {"k": "app", "fn": {"k": "const", "n": "List", "us": "[0]"}, "arg": _nat()}


def _app(function: dict[str, object], argument: dict[str, object]) -> dict[str, object]:
    return {"k": "app", "fn": function, "arg": argument}


def _membership(*, member_index: int = 0) -> dict[str, object]:
    node: dict[str, object] = {"k": "const", "n": "Membership.mem", "us": "[0, 0]"}
    for argument in (
        _nat(),
        _list_nat(),
        _app({"k": "const", "n": "List.instMembership", "us": "[0]"}, _nat()),
        {"k": "bvar", "i": 1},
        {"k": "bvar", "i": member_index},
    ):
        node = _app(node, argument)
    return node


def _eq(*, x_index: int) -> dict[str, object]:
    node: dict[str, object] = {"k": "const", "n": "Eq", "us": "[1]"}
    for argument in (_nat(), {"k": "bvar", "i": x_index}, {"k": "lit", "nat": "0"}):
        node = _app(node, argument)
    return node


def _root(
    *,
    guarded: bool = True,
    member_index: int = 0,
    body_uses_x: bool = True,
    multiple_guards: bool = False,
) -> dict[str, object]:
    predicate: dict[str, object] = (
        _eq(x_index=1 if guarded else 0) if body_uses_x else {"k": "const", "n": "True", "us": "[]"}
    )
    if guarded and multiple_guards:
        predicate = {
            "k": "forall",
            "bi": "default",
            "dom": _membership(member_index=1),
            "body": _eq(x_index=2),
        }
    target_body = (
        {
            "k": "forall",
            "bi": "default",
            "dom": _membership(member_index=member_index),
            "body": predicate,
        }
        if guarded
        else predicate
    )
    return {
        "k": "forall",
        "bi": "default",
        "dom": _list_nat(),
        "body": {
            "k": "forall",
            "bi": "default",
            "dom": _nat(),
            "body": target_body,
        },
    }


def _records(
    source: str,
    key: str,
    root: dict[str, object],
) -> tuple[TheoremRecord, RepresentationRecord]:
    theorem_id = make_id("thm", {"n16": key})
    ancestry_id = make_id("anc", {"n16": key})
    theorem = theorem_record(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        declaration_name="n16",
        declaration_full_name="n16",
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
        representation_id=make_id("repr", {"n16": key}),
        theorem_id=theorem_id,
        raw_proof_stripped=source,
        headless="(s : List Nat) : ∀ x : Nat, x ∈ s → x = 0",
        signature_pp="(s : List Nat) : ∀ x : Nat, x ∈ s → x = 0",
        signature_explicit="∀ (s : List Nat) (x : Nat), Membership.mem s x → Eq x 0",
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
    candidate_root = _root(guarded=False)
    candidate_representation = source_representation.model_copy(
        update={
            "representation_id": make_id("repr", {"n16_candidate": candidate.theorem_id}),
            "theorem_id": candidate.theorem_id,
            "raw_proof_stripped": draft.candidate_code,
            "headless": "(s : List Nat) : ∀ x : Nat, x = 0",
            "signature_pp": "(s : List Nat) : ∀ x : Nat, x = 0",
            "signature_explicit": "∀ (s : List Nat) (x : Nat), Eq x 0",
            "semantic_atoms": semantic_atoms(candidate_root),
            "operator_tree": operator_tree(candidate_root),
            "alpha_identity_fingerprint": alpha_identity_fingerprint(candidate_root),
        }
    )
    return candidate, candidate_representation


def test_n16_enumerates_exact_root_guard_and_builds_expected_tree() -> None:
    (site,) = enumerate_n16_sites(_SOURCE, operator_tree(_root()))
    assert site.variable_name == "x"
    assert site.binder_type_text == "Nat"
    assert site.guard_text == "s"
    assert (
        apply_n16_trace(
            _SOURCE,
            (
                {
                    "operation": "replace_exact_span",
                    "start": site.edit_start,
                    "end": site.edit_end,
                    "expected_text": site.source_text,
                    "replacement_text": site.replacement_text,
                },
            ),
        )
        == _CANDIDATE
    )
    assert build_domain_guard_removal_root(_root(), 1) == _root(guarded=False)


def test_n16_rejects_wrong_member_unused_body_and_multiple_guards() -> None:
    for root, message in (
        (_root(member_index=1), "membership_does_not_guard"),
        (_root(body_uses_x=False), "predicate_does_not_use"),
        (_root(multiple_guards=True), "multiple_membership_guards"),
    ):
        with pytest.raises(N16DomainGuardError, match=message):
            build_domain_guard_removal_root(root, 1)
        assert enumerate_n16_sites(_SOURCE, operator_tree(root)) == ()


def test_n16_surface_scope_supports_bounded_notation_and_rejects_nested_target() -> None:
    bounded = "theorem n16 (s : List Nat) : ∀ x ∈ s, x = 0 := by sorry"
    nested = "theorem n16 (s : List Nat) : True → ∀ x : Nat, x ∈ s → x = 0 := by sorry"
    (site,) = enumerate_n16_sites(bounded, operator_tree(_root()))
    assert site.surface_form == "bounded_notation"
    assert site.binder_type_text == "<inferred>"
    trace = (
        {
            "operation": "replace_exact_span",
            "start": site.edit_start,
            "end": site.edit_end,
            "expected_text": site.source_text,
            "replacement_text": site.replacement_text,
        },
    )
    assert apply_n16_trace(bounded, trace) == (
        "theorem n16 (s : List Nat) : ∀ x, x = 0 := by sorry"
    )
    assert enumerate_n16_sites(nested, operator_tree(_root())) == ()


def test_n16_generation_is_invertible_and_provisional() -> None:
    theorem, representation = _records(_SOURCE, "generate", _root())
    rule = N16DomainGuardRule(generation_config_hash="d" * 64, candidate_pool="fixture")
    (draft,) = rule.generate(theorem, representation, seed=16)
    assert draft.candidate_code == _CANDIDATE
    assert draft.intended_relation == IntendedRelation.NEAR_MISS
    assert draft.intended_error_types == ("E01", "E20", "E26")
    assert draft.inverse_trace is not None
    assert apply_n16_trace(draft.candidate_code, draft.inverse_trace) == _SOURCE
    assert draft.metadata["resolved_semantic_label"] is False
    assert draft.metadata["training_eligible"] is False


def test_n16_certificate_accepts_only_exact_guard_removal() -> None:
    certificate = certify_domain_guard_removal(_root(), _root(guarded=False), 1)
    assert certificate.header_binder_count == 1
    with pytest.raises(N16DomainGuardError, match="candidate_not_exact"):
        certify_domain_guard_removal(_root(), _root(), 1)


def test_n16_clean_audit_remains_provisional() -> None:
    theorem, representation = _records(_SOURCE, "audit", _root())
    rule = N16DomainGuardRule(generation_config_hash="d" * 64, candidate_pool="fixture")
    (draft,) = rule.generate(theorem, representation, seed=16)
    candidate, candidate_representation = _candidate_records(theorem, representation, draft)
    audit = rule.audit(
        theorem,
        representation,
        candidate,
        candidate_representation,
        draft,
    )
    assert audit.violation_codes == ()
    assert audit.recommended_quality_tier == QualityTier.PROVISIONAL
    assert audit.structural_diff_ok is True
    assert audit.inverse_or_roundtrip_ok is True
    assert audit.metadata["evidence_class"] == "D0"
    assert audit.metadata["failed_proof_search_used"] is False
    assert audit.metadata["resolved_semantic_label"] is False
    assert audit.metadata["training_eligible"] is False


def test_n16_runtime_is_closed_and_config_bound() -> None:
    loaded = load_v2_d0_n16_execution_config()
    runtime = build_v2_d0_n16_runtime()
    assert loaded.config.profile_id == "deterministic_v2_d0_n16_experimental"
    assert runtime.rule_ids == ("n16_domain_guard_removal",)
    theorem, representation = _records(_SOURCE, "runtime", _root())
    execution = runtime.execute("n16_domain_guard_removal", theorem, representation, seed=16)
    assert execution.attempt.terminal_outcome == "generated"
    assert len(execution.drafts) == 1
    with pytest.raises(V2D0N16ExecutionError, match="outside"):
        runtime.execute("n15_conjunct_omission", theorem, representation, seed=16)


def test_n16_trace_corruption_fails_closed() -> None:
    theorem, representation = _records(_SOURCE, "trace", _root())
    rule = N16DomainGuardRule(generation_config_hash="d" * 64, candidate_pool="fixture")
    (draft,) = rule.generate(theorem, representation, seed=16)
    corrupted = dict(draft.transformation_trace[0])
    corrupted["expected_text"] = "wrong"
    with pytest.raises(N16DomainGuardError, match="expected_text_mismatch"):
        apply_n16_trace(_SOURCE, (corrupted,))

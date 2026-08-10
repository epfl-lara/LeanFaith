"""Focused LF-034 tests for the provisional N17 D0 family."""

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
from leanfaith.transforms.negatives.n17_role_arguments import (
    N17RoleArgumentError,
    N17RoleArgumentRule,
    apply_n17_trace,
    build_role_argument_swap_root,
    certify_role_argument_swap,
    enumerate_n17_sites,
)
from leanfaith.transforms.v2_d0_n17_runtime import (
    V2D0N17ExecutionError,
    build_v2_d0_n17_runtime,
    load_v2_d0_n17_execution_config,
)
from tests.unit.record_factories import representation_record, theorem_record

_SOURCE = "theorem n17 (p q : Nat) : p > q := by sorry"
_CANDIDATE = "theorem n17 (p q : Nat) : q > p := by sorry"


def _nat() -> dict[str, object]:
    return {"k": "const", "n": "Nat", "us": "[]"}


def _app(function: dict[str, object], argument: dict[str, object]) -> dict[str, object]:
    return {"k": "app", "fn": function, "arg": argument}


def _relation(
    *,
    head: str = "GT.gt",
    left_index: int = 1,
    right_index: int = 0,
) -> dict[str, object]:
    node: dict[str, object] = {"k": "const", "n": head, "us": "[0]"}
    for argument in (
        _nat(),
        {"k": "const", "n": "Nat.instLTNat", "us": "[]"},
        {"k": "bvar", "i": left_index},
        {"k": "bvar", "i": right_index},
    ):
        node = _app(node, argument)
    return node


def _root(
    *,
    head: str = "GT.gt",
    swapped: bool = False,
    second_domain: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "k": "forall",
        "bi": "default",
        "dom": _nat(),
        "body": {
            "k": "forall",
            "bi": "default",
            "dom": second_domain or _nat(),
            "body": _relation(
                head=head,
                left_index=0 if swapped else 1,
                right_index=1 if swapped else 0,
            ),
        },
    }


def _records(
    source: str,
    key: str,
    root: dict[str, object],
) -> tuple[TheoremRecord, RepresentationRecord]:
    theorem_id = make_id("thm", {"n17": key})
    ancestry_id = make_id("anc", {"n17": key})
    theorem = theorem_record(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        declaration_name="n17",
        declaration_full_name="n17",
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
        representation_id=make_id("repr", {"n17": key}),
        theorem_id=theorem_id,
        raw_proof_stripped=source,
        headless="(p q : Nat) : p > q",
        signature_pp="(p q : Nat) : p > q",
        signature_explicit="∀ (p q : Nat), GT.gt p q",
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
            "representation_id": make_id("repr", {"n17_candidate": candidate.theorem_id}),
            "theorem_id": candidate.theorem_id,
            "raw_proof_stripped": draft.candidate_code,
            "headless": "(p q : Nat) : q > p",
            "signature_pp": "(p q : Nat) : q > p",
            "signature_explicit": "∀ (p q : Nat), GT.gt q p",
            "semantic_atoms": semantic_atoms(candidate_root),
            "operator_tree": operator_tree(candidate_root),
            "alpha_identity_fingerprint": alpha_identity_fingerprint(candidate_root),
        }
    )
    return candidate, candidate_representation


def test_n17_enumerates_exact_allowlisted_root_relation() -> None:
    (site,) = enumerate_n17_sites(_SOURCE, operator_tree(_root()))
    assert (site.left_name, site.right_name) == ("p", "q")
    assert site.surface_operator == ">"
    assert site.elaborated_head == "GT.gt"
    assert (site.left_outer_index, site.right_outer_index) == (0, 1)
    assert build_role_argument_swap_root(_root(), 2, "GT.gt") == _root(swapped=True)


def test_n17_trace_round_trip_supports_unicode_binder_names() -> None:
    source = "theorem n17 (y₁ y₂ : Nat) : y₁ > y₂ := by sorry"
    (site,) = enumerate_n17_sites(source, operator_tree(_root()))
    forward = (
        {
            "operation": "swap_exact_spans",
            "left_start": site.left_start,
            "left_end": site.left_end,
            "left_text": site.left_name,
            "right_start": site.right_start,
            "right_end": site.right_end,
            "right_text": site.right_name,
        },
    )
    candidate = apply_n17_trace(source, forward)
    assert candidate == "theorem n17 (y₁ y₂ : Nat) : y₂ > y₁ := by sorry"


def test_n17_rejects_symmetric_nested_and_nonbinder_relations() -> None:
    symmetric = "theorem n17 (p q : Nat) : p = q := by sorry"
    nested = "theorem n17 (p q : Nat) : p > q ∧ True := by sorry"
    expression = "theorem n17 (p q : Nat) : p + 1 > q := by sorry"
    assert enumerate_n17_sites(symmetric, operator_tree(_root(head="Eq"))) == ()
    assert enumerate_n17_sites(nested, operator_tree(_root())) == ()
    assert enumerate_n17_sites(expression, operator_tree(_root())) == ()


def test_n17_rejects_different_or_role_dependent_binder_domains() -> None:
    different = "theorem n17 (p : Nat) (q : Int) : p > q := by sorry"
    dependent = "theorem n17 (p : Nat) (q : Fin p) : p > q := by sorry"
    int_domain = {"k": "const", "n": "Int", "us": "[]"}
    dependent_domain = {
        "k": "app",
        "fn": {"k": "const", "n": "Fin", "us": "[]"},
        "arg": {"k": "bvar", "i": 0},
    }
    assert enumerate_n17_sites(different, operator_tree(_root(second_domain=int_domain))) == ()
    assert (
        enumerate_n17_sites(dependent, operator_tree(_root(second_domain=dependent_domain))) == ()
    )


def test_n17_generation_is_invertible_and_provisional() -> None:
    theorem, representation = _records(_SOURCE, "generate", _root())
    rule = N17RoleArgumentRule(generation_config_hash="d" * 64, candidate_pool="fixture")
    (draft,) = rule.generate(theorem, representation, seed=17)
    assert draft.candidate_code == _CANDIDATE
    assert draft.intended_relation == IntendedRelation.NEAR_MISS
    assert draft.intended_error_types == ("E12", "E30")
    assert draft.inverse_trace is not None
    assert apply_n17_trace(draft.candidate_code, draft.inverse_trace) == _SOURCE
    assert draft.metadata["resolved_semantic_label"] is False
    assert draft.metadata["training_eligible"] is False


def test_n17_certificate_accepts_only_exact_argument_swap() -> None:
    certificate = certify_role_argument_swap(_root(), _root(swapped=True), 2, "GT.gt")
    assert certificate.elaborated_head == "GT.gt"
    with pytest.raises(N17RoleArgumentError, match="candidate_not_exact"):
        certify_role_argument_swap(_root(), _root(), 2, "GT.gt")


def test_n17_clean_audit_remains_provisional() -> None:
    theorem, representation = _records(_SOURCE, "audit", _root())
    rule = N17RoleArgumentRule(generation_config_hash="e" * 64, candidate_pool="fixture")
    (draft,) = rule.generate(theorem, representation, seed=17)
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
    assert audit.metadata["resolved_semantic_label"] is False
    assert audit.metadata["training_eligible"] is False


def test_n17_tampered_candidate_is_quarantined() -> None:
    theorem, representation = _records(_SOURCE, "tamper", _root())
    rule = N17RoleArgumentRule(generation_config_hash="f" * 64, candidate_pool="fixture")
    (draft,) = rule.generate(theorem, representation, seed=17)
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
    assert "candidate_not_exact_role_argument_swap" in audit.violation_codes
    assert audit.recommended_quality_tier == QualityTier.UNKNOWN


def test_n17_runtime_profile_is_closed_and_dispatches_only_n17() -> None:
    loaded = load_v2_d0_n17_execution_config()
    assert loaded.config.profile_id == "deterministic_v2_d0_n17_experimental"
    assert loaded.config.training_eligible is False
    runtime = build_v2_d0_n17_runtime()
    theorem, representation = _records(_SOURCE, "runtime", _root())
    execution = runtime.execute("n17_role_sensitive_arguments", theorem, representation, 17)
    assert execution.attempt.terminal_outcome == "generated"
    assert len(execution.drafts) == 1
    with pytest.raises(V2D0N17ExecutionError, match="outside the N17 profile"):
        runtime.execute("n16_domain_guard_removal", theorem, representation, 17)

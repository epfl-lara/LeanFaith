"""Focused LF-033 tests for provisional P14 binder permutation."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from leanfaith.representations import alpha_identity_fingerprint
from leanfaith.representations.atoms import operator_tree, semantic_atoms
from leanfaith.schemas import CANONICAL_VIEW_NAMES
from leanfaith.schemas.enums import IntendedRelation, ValidationStatus, ViewStatus
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.schemas.variant import VariantDraft
from leanfaith.transforms.materialize import build_derived_theorem_record
from leanfaith.transforms.positives.p14_binder_permutation import (
    P14BinderPermutationError,
    P14BinderPermutationRule,
    apply_p14_trace,
    build_binder_permutation_root,
    certify_binder_permutation,
    enumerate_p14_surface_sites,
)
from leanfaith.transforms.v2_e2_p14_runtime import (
    V2E2P14Runtime,
    build_v2_e2_p14_runtime,
)
from leanfaith.transforms.v2_e2_runtime import build_v2_e2_runtime
from tests.unit.record_factories import representation_record, theorem_record


def _sort(universe: str) -> dict[str, object]:
    return {"k": "sort", "u": universe}


def _const(name: str) -> dict[str, object]:
    return {"k": "const", "n": name, "us": "[]"}


def _bvar(index: int) -> dict[str, object]:
    return {"k": "bvar", "i": index}


def _app(function: dict[str, object], argument: dict[str, object]) -> dict[str, object]:
    return {"k": "app", "fn": function, "arg": argument}


def _eq(
    type_node: dict[str, object],
    left: dict[str, object],
    right: dict[str, object],
) -> dict[str, object]:
    return _app(_app(_app(_const("Eq"), type_node), left), right)


def _forall(
    domain: dict[str, object],
    body: dict[str, object],
    *,
    binder_info: str = "default",
) -> dict[str, object]:
    return {"k": "forall", "bi": binder_info, "dom": domain, "body": body}


def _nat_pair_root(*, hidden_prefix: bool = False) -> dict[str, object]:
    # Under x,y the conclusion x = y uses x=bvar1 and y=bvar0.
    body = _eq(_const("Nat"), _bvar(1), _bvar(0))
    result = _forall(_const("Nat"), _forall(_const("Nat"), body))
    return _forall(_sort("1"), result, binder_info="implicit") if hidden_prefix else result


def _generic_pair_root() -> dict[str, object]:
    # A : Type, x : A, y : A |- x = y.  The right domain refers past x to A.
    body = _eq(_bvar(2), _bvar(1), _bvar(0))
    return _forall(_sort("1"), _forall(_bvar(0), _forall(_bvar(1), body)))


def _proof_pair_root() -> dict[str, object]:
    # P : Prop, h1 : P, h2 : P |- h1 = h2.  h1/h2 are proof binders.
    body = _eq(_bvar(2), _bvar(1), _bvar(0))
    return _forall(_sort("0"), _forall(_bvar(0), _forall(_bvar(1), body)))


def _records(source: str, root: dict[str, object], key: str):
    theorem_id = make_id("thm", {"p14": key})
    ancestry_id = make_id("anc", {"p14": key})
    theorem = theorem_record(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        declaration_name="p14",
        declaration_full_name="p14",
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
        representation_id=make_id("repr", {"p14": key}),
        theorem_id=theorem_id,
        raw_proof_stripped=source,
        headless=source,
        signature_pp=source,
        signature_explicit=source,
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
    *,
    candidate_root: dict[str, object],
) -> tuple[TheoremRecord, RepresentationRecord]:
    candidate = build_derived_theorem_record(
        draft=draft,
        sources=(source,),
        primary_source_id=source.theorem_id,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        inline_elaboration_source="import LeanFaithFixtures\n" + draft.candidate_code,
    )
    candidate_representation = source_representation.model_copy(
        update={
            "representation_id": make_id("repr", {"p14_candidate": candidate.theorem_id}),
            "theorem_id": candidate.theorem_id,
            "raw_proof_stripped": draft.candidate_code,
            "headless": draft.candidate_code,
            "signature_pp": draft.candidate_code,
            "signature_explicit": draft.candidate_code,
            "semantic_atoms": semantic_atoms(candidate_root),
            "operator_tree": operator_tree(candidate_root),
            "alpha_identity_fingerprint": alpha_identity_fingerprint(candidate_root),
        }
    )
    return candidate, candidate_representation


def test_p14_grouped_and_singleton_surface_edits_round_trip_exactly() -> None:
    grouped = "theorem p14 (longName y : Nat) : longName = y := by sorry"
    grouped_sites = enumerate_p14_surface_sites(grouped)
    assert len(grouped_sites) == 1
    grouped_site = grouped_sites[0]
    assert grouped_site.operation == "swap_within_typed_group"
    assert grouped_site.p02_site_count == 1

    singleton = "theorem p14 (x : Nat)   (longName : Int) : True := by sorry"
    singleton_sites = enumerate_p14_surface_sites(singleton)
    assert len(singleton_sites) == 1
    singleton_site = singleton_sites[0]
    assert singleton_site.operation == "swap_adjacent_singletons"
    assert singleton_site.candidate_text == "(longName : Int)   (x : Nat)"
    assert singleton_site.p02_site_count == 0

    same_type_singletons = enumerate_p14_surface_sites(
        "theorem p14 (x : Nat) (y : Nat) : x = y := by sorry"
    )
    assert len(same_type_singletons) == 1
    assert same_type_singletons[0].p02_site_count == 1

    rule = P14BinderPermutationRule(generation_config_hash="a" * 64, candidate_pool="fixture")
    theorem, representation = _records(
        "theorem p14 (x y : Nat) : x = y := by sorry",
        _nat_pair_root(),
        "roundtrip",
    )
    (draft,) = rule.generate(theorem, representation, seed=14)
    assert draft.intended_relation == IntendedRelation.EQUIVALENT
    assert draft.candidate_code == "theorem p14 (y x : Nat) : x = y := by sorry"
    assert draft.inverse_trace is not None
    assert (
        apply_p14_trace(draft.candidate_code, draft.inverse_trace)
        == theorem.proof_stripped_declaration
    )
    assert draft.metadata["resolved_semantic_label"] is False
    assert draft.metadata["training_eligible"] is False


def test_p14_exact_debruijn_permutation_is_an_involution() -> None:
    source = _generic_pair_root()
    candidate = build_binder_permutation_root(source, 1)
    assert build_binder_permutation_root(candidate, 1) == source
    assert candidate != source

    source_text = "theorem p14 (A : Type) (x y : A) : x = y := by sorry"
    (site,) = enumerate_p14_surface_sites(source_text)
    certificate = certify_binder_permutation(
        source,
        candidate,
        site=site,
        source_alpha_fingerprint=alpha_identity_fingerprint(source),
        candidate_alpha_fingerprint=alpha_identity_fingerprint(candidate),
    )
    assert certificate.selected_outer_indices == (1, 2)
    assert certificate.left_data_evidence == "typed_bvar"
    assert certificate.right_data_evidence == "typed_bvar"
    assert certificate.right_domain_free_bvars == (1,)
    assert certificate.source_residual_hash == certificate.inverse_residual_hash


def test_p14_unique_full_tree_match_supports_hidden_prefix() -> None:
    source = _nat_pair_root(hidden_prefix=True)
    candidate = build_binder_permutation_root(source, 1)
    source_text = "theorem p14 (x y : Nat) : x = y := by sorry"
    (site,) = enumerate_p14_surface_sites(source_text)
    certificate = certify_binder_permutation(
        source,
        candidate,
        site=site,
        source_alpha_fingerprint=alpha_identity_fingerprint(source),
        candidate_alpha_fingerprint=alpha_identity_fingerprint(candidate),
    )
    assert certificate.selected_outer_indices == (1, 2)
    assert certificate.hidden_outer_offset == 1


def test_p14_rejects_proof_dependent_implicit_unused_and_unsafe_sites() -> None:
    with pytest.raises(P14BinderPermutationError, match=r"unsupported_data_domain|proof_binder"):
        build_binder_permutation_root(_proof_pair_root(), 1)

    dependent = _forall(
        _const("Nat"),
        _forall(_bvar(0), _eq(_bvar(1), _bvar(1), _bvar(0))),
    )
    with pytest.raises(P14BinderPermutationError, match="right_domain_depends_on_left"):
        build_binder_permutation_root(dependent, 0)

    unused = _forall(_const("Nat"), _forall(_const("Nat"), _eq(_const("Nat"), _bvar(0), _bvar(0))))
    with pytest.raises(P14BinderPermutationError, match="selected_binder_unused"):
        build_binder_permutation_root(unused, 0)

    implicit = _forall(
        _const("Nat"),
        _forall(_const("Nat"), _eq(_const("Nat"), _bvar(1), _bvar(0))),
        binder_info="implicit",
    )
    with pytest.raises(P14BinderPermutationError, match="nonexplicit"):
        build_binder_permutation_root(implicit, 0)

    assert enumerate_p14_surface_sites("theorem p14 (x /- no -/ y : Nat) : x = y := by sorry") == ()
    assert enumerate_p14_surface_sites("theorem p14 (x x : Nat) : x = x := by sorry") == ()
    assert enumerate_p14_surface_sites("theorem p14 {x y : Nat} : x = y := by sorry") == ()


def test_p14_runtime_binds_separate_zero_credit_profile() -> None:
    root = Path(__file__).parents[2]
    runtime = build_v2_e2_runtime(
        root,
        path=root / "configs/transformations/v2_e2_p14_experimental.yaml",
    )
    assert isinstance(runtime, V2E2P14Runtime)
    assert runtime.rule_ids == ("p14_independent_binder_permutation",)
    theorem, representation = _records(
        "theorem p14 (x y : Nat) : x = y := by sorry", _nat_pair_root(), "runtime"
    )
    execution = build_v2_e2_p14_runtime().execute(
        "p14_independent_binder_permutation", theorem, representation, seed=14
    )
    assert execution.attempt.terminal_outcome == "generated"
    assert len(execution.drafts) == 1


def test_p14_trace_corruption_fails_closed() -> None:
    source = "theorem p14 (x y : Nat) : x = y := by sorry"
    theorem, representation = _records(source, _nat_pair_root(), "corrupt")
    rule = P14BinderPermutationRule(generation_config_hash="b" * 64, candidate_pool="fixture")
    (draft,) = rule.generate(theorem, representation, seed=14)
    corrupted = (dict(draft.transformation_trace[0], expected_text="drift"),)
    with pytest.raises(P14BinderPermutationError, match="trace_expected_text_mismatch"):
        apply_p14_trace(source, corrupted)


def test_p14_audit_recomputes_alpha_fingerprints_and_fails_closed_on_drift() -> None:
    source_text = "theorem p14 (x y : Nat) : x = y := by sorry"
    source_root = _nat_pair_root()
    theorem, representation = _records(source_text, source_root, "fingerprint-drift")
    rule = P14BinderPermutationRule(generation_config_hash="c" * 64, candidate_pool="fixture")
    (draft,) = rule.generate(theorem, representation, seed=14)
    candidate, candidate_representation = _candidate_records(
        theorem,
        representation,
        draft,
        candidate_root=build_binder_permutation_root(source_root, 0),
    )
    clean = rule.audit(theorem, representation, candidate, candidate_representation, draft)
    assert clean.violation_codes == ()

    corrupted = candidate_representation.model_copy(update={"alpha_identity_fingerprint": "0" * 64})
    rejected = rule.audit(theorem, representation, candidate, corrupted, draft)
    assert "candidate_alpha_fingerprint_mismatch" in rejected.violation_codes


def test_p14_tree_transform_preserves_nested_binder_cutoffs() -> None:
    # The local lambda's bvar 0 must remain local while outer x/y roles swap.
    residual: dict[str, Any] = {
        "k": "lam",
        "bi": "default",
        "dom": _const("Nat"),
        "body": _eq(_const("Nat"), _bvar(2), _bvar(1)),
    }
    source = _forall(_const("Nat"), _forall(_const("Nat"), residual))
    candidate = build_binder_permutation_root(source, 0)
    assert build_binder_permutation_root(candidate, 0) == source

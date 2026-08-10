"""Focused LF-034 tests for the provisional N11 D0 family."""

from __future__ import annotations

import hashlib

import pytest

from leanfaith.representations import alpha_identity_fingerprint
from leanfaith.representations.atoms import operator_tree, semantic_atoms
from leanfaith.schemas import CANONICAL_VIEW_NAMES, ViewStatus, make_id
from leanfaith.schemas.enums import IntendedRelation, QualityTier, ValidationStatus
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms.materialize import build_derived_theorem_record
from leanfaith.transforms.negatives.n11_bound_variable import (
    N11BoundVariableError,
    N11BoundVariableRule,
    apply_n11_trace,
    certify_single_bvar_delta,
    enumerate_n11_sites,
)
from leanfaith.transforms.v2_d0_runtime import (
    V2D0ExecutionError,
    build_v2_d0_runtime,
    load_v2_d0_execution_config,
)
from tests.unit.record_factories import representation_record, theorem_record

_SOURCE = "theorem n11 (x y : Nat) : x = y := by sorry"


def _root(left_index: int, right_index: int) -> dict[str, object]:
    nat = {"k": "const", "n": "Nat", "us": "[]"}
    return {
        "k": "forall",
        "bi": "default",
        "dom": nat,
        "body": {
            "k": "forall",
            "bi": "default",
            "dom": nat,
            "body": {
                "k": "app",
                "fn": {
                    "k": "app",
                    "fn": {
                        "k": "app",
                        "fn": {"k": "const", "n": "Eq", "us": "[0]"},
                        "arg": nat,
                    },
                    "arg": {"k": "bvar", "i": left_index},
                },
                "arg": {"k": "bvar", "i": right_index},
            },
        },
    }


def _records(
    source: str,
    key: str,
    root: dict[str, object],
) -> tuple[TheoremRecord, RepresentationRecord]:
    theorem_id = make_id("thm", {"n11": key})
    ancestry_id = make_id("anc", {"n11": key})
    theorem = theorem_record(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        declaration_name="n11",
        declaration_full_name="n11",
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
        representation_id=make_id("repr", {"n11": key}),
        theorem_id=theorem_id,
        raw_proof_stripped=source,
        headless="(x y : Nat) : x = y",
        signature_pp="∀ (x y : Nat), x = y",
        signature_explicit="∀ (x y : Nat), @Eq Nat x y",
        semantic_atoms=semantic_atoms(root),
        operator_tree=operator_tree(root),
        alpha_identity_fingerprint=alpha_identity_fingerprint(root),
        view_status=statuses,
    )
    return theorem, representation


def _candidate_records(
    source: TheoremRecord,
    source_representation: RepresentationRecord,
    draft_code: str,
    root: dict[str, object],
) -> tuple[TheoremRecord, RepresentationRecord]:
    rule = N11BoundVariableRule(generation_config_hash="a" * 64, candidate_pool="fixture")
    draft = rule.generate(source, source_representation, seed=7)[0]
    assert draft.candidate_code == draft_code
    candidate = build_derived_theorem_record(
        draft=draft,
        sources=(source,),
        primary_source_id=source.theorem_id,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        inline_elaboration_source="import LeanFaithFixtures\n" + draft_code,
    )
    candidate_representation = source_representation.model_copy(
        update={
            "representation_id": make_id("repr", {"n11_candidate": candidate.theorem_id}),
            "theorem_id": candidate.theorem_id,
            "raw_proof_stripped": draft_code,
            "semantic_atoms": semantic_atoms(root),
            "operator_tree": operator_tree(root),
            "alpha_identity_fingerprint": alpha_identity_fingerprint(root),
        }
    )
    return candidate, candidate_representation


def test_n11_enumerates_same_typed_explicit_conclusion_occurrences() -> None:
    sites = enumerate_n11_sites(_SOURCE)
    assert [(site.source_name, site.target_name) for site in sites] == [
        ("x", "y"),
        ("y", "x"),
    ]
    assert all(site.type_token_hash == sites[0].type_token_hash for site in sites)


@pytest.mark.parametrize(
    "source",
    [
        "theorem one (x : Nat) : x = x := by sorry",
        "theorem types (x : Nat) (y : Int) : x = x := by sorry",
        "theorem implicit {x y : Nat} : x = y := by sorry",
        "theorem instance [x : Inhabited Nat] [y : Inhabited Nat] : True := by sorry",
        # The data binders depend on the preceding type binder and are outside
        # the deliberately narrow N11 v1 certificate.
        "theorem dependent (α : Type) (x y : α) : x = y := by sorry",
    ],
)
def test_n11_rejects_single_different_implicit_instance_and_dependent_binders(
    source: str,
) -> None:
    assert enumerate_n11_sites(source) == ()


def test_n11_generation_is_seeded_exact_and_inverse_replayable() -> None:
    theorem, representation = _records(_SOURCE, "generate", _root(1, 0))
    rule = N11BoundVariableRule(generation_config_hash="a" * 64, candidate_pool="fixture")
    draft = rule.generate(theorem, representation, seed=7)[0]
    assert draft.candidate_code in {
        "theorem n11 (x y : Nat) : y = y := by sorry",
        "theorem n11 (x y : Nat) : x = x := by sorry",
    }
    assert draft.intended_relation == IntendedRelation.NEAR_MISS
    assert draft.intended_error_types == ("E16", "E26")
    assert draft.metadata["resolved_semantic_label"] is False
    assert draft.metadata["training_eligible"] is False
    assert draft.inverse_trace is not None
    assert apply_n11_trace(draft.candidate_code, draft.inverse_trace) == _SOURCE
    assert rule.generate(theorem, representation, seed=7) == (draft,)


def test_n11_structural_certificate_accepts_exactly_one_same_typed_bvar_delta() -> None:
    certificate = certify_single_bvar_delta(_root(1, 0), _root(1, 1))
    assert certificate.source_index == 0
    assert certificate.target_index == 1
    assert certificate.path.endswith("/arg")
    assert len(certificate.domain_hash) == 64


def test_n11_structural_certificate_rejects_multiple_or_different_typed_deltas() -> None:
    with pytest.raises(N11BoundVariableError, match="expected_exactly_one_bvar_delta"):
        certify_single_bvar_delta(_root(1, 0), _root(0, 1))

    nat = _root(1, 0)
    different_domain = _root(1, 1)
    assert isinstance(different_domain["body"], dict)
    different_domain["body"]["dom"] = {"k": "const", "n": "Int", "us": "[]"}
    with pytest.raises(N11BoundVariableError, match="non_bvar_structural_delta"):
        certify_single_bvar_delta(nat, different_domain)


def test_n11_clean_audit_is_d0_provisional_without_semantic_credit() -> None:
    theorem, representation = _records(_SOURCE, "audit", _root(1, 0))
    rule = N11BoundVariableRule(generation_config_hash="a" * 64, candidate_pool="fixture")
    draft = rule.generate(theorem, representation, seed=7)[0]
    if draft.candidate_code.endswith("y = y := by sorry"):
        candidate_root = _root(0, 0)
    else:
        candidate_root = _root(1, 1)
    candidate, candidate_representation = _candidate_records(
        theorem,
        representation,
        draft.candidate_code,
        candidate_root,
    )
    audit = rule.audit(
        theorem,
        representation,
        candidate,
        candidate_representation,
        draft,
    )
    assert audit.violation_codes == ()
    assert audit.recommended_quality_tier == QualityTier.PROVISIONAL
    assert audit.recommended_validation_status == ValidationStatus.ELABORATES_WITH_PLACEHOLDER
    assert audit.structural_diff_ok is True
    assert audit.atom_mapping_ok is True
    assert audit.inverse_or_roundtrip_ok is True
    assert audit.metadata["evidence_class"] == "D0"
    assert audit.metadata["failed_proof_search_used"] is False
    assert audit.metadata["resolved_semantic_label"] is False
    assert audit.metadata["training_eligible"] is False


def test_n11_audit_quarantines_atom_or_structure_corruption() -> None:
    theorem, representation = _records(_SOURCE, "corrupt", _root(1, 0))
    rule = N11BoundVariableRule(generation_config_hash="a" * 64, candidate_pool="fixture")
    draft = rule.generate(theorem, representation, seed=7)[0]
    candidate_root = (
        _root(0, 0) if draft.candidate_code.endswith("y = y := by sorry") else _root(1, 1)
    )
    candidate, candidate_representation = _candidate_records(
        theorem,
        representation,
        draft.candidate_code,
        candidate_root,
    )
    assert candidate_representation.semantic_atoms is not None
    corrupted = candidate_representation.model_copy(
        update={"semantic_atoms": (*candidate_representation.semantic_atoms, "const:False")}
    )
    audit = rule.audit(theorem, representation, candidate, corrupted, draft)
    assert audit.recommended_quality_tier == QualityTier.UNKNOWN
    assert "semantic_atoms_changed" in audit.violation_codes


def test_n11_trace_corruption_fails_closed() -> None:
    theorem, representation = _records(_SOURCE, "trace", _root(1, 0))
    rule = N11BoundVariableRule(generation_config_hash="a" * 64, candidate_pool="fixture")
    draft = rule.generate(theorem, representation, seed=7)[0]
    step = {**draft.transformation_trace[0], "expected_text": "not_the_source"}
    with pytest.raises(N11BoundVariableError, match="expected text mismatch"):
        apply_n11_trace(_SOURCE, (step,))


def test_n11_profile_binds_portfolio_and_dispatches_only_n11() -> None:
    loaded = load_v2_d0_execution_config()
    assert loaded.config.profile_id == "deterministic_v2_d0_n11_experimental"
    assert loaded.config.active_rules[0].intended_error_types == ("E16", "E26")
    assert loaded.config.resolved_label_count == 0
    assert loaded.config.promoted_item_count == 0
    assert loaded.config.training_eligible is False

    theorem, representation = _records(_SOURCE, "runtime", _root(1, 0))
    runtime = build_v2_d0_runtime()
    execution = runtime.execute(
        "n11_bound_variable_substitution",
        theorem,
        representation,
        seed=7,
    )
    assert execution.attempt.terminal_outcome == "generated"
    assert len(execution.drafts) == 1
    with pytest.raises(V2D0ExecutionError, match="outside the N11 profile"):
        runtime.execute("n12_implication_converse", theorem, representation, seed=7)

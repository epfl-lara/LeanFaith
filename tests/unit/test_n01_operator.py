"""LF-018 N01 finite type-aware operator replacement tests."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from pydantic import ValidationError

from leanfaith.config.hashing import hash_canonical
from leanfaith.schemas import (
    CANONICAL_VIEW_NAMES,
    IntendedRelation,
    QualityTier,
    ValidationStatus,
    ViewStatus,
    make_id,
)
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms.n01_operator import (
    N01OperatorError,
    N01OperatorRule,
    N01ReplacementEntry,
    apply_operator_trace,
    enumerate_operator_sites,
    load_n01_operator_config,
)
from tests.unit.record_factories import (
    REPR_A,
    THM_A,
    representation_record,
    theorem_record,
)

_GENERATION_HASH = "4" * 64
_SOURCE_ALPHA = "5" * 64
_CANDIDATE_ALPHA = "6" * 64
_SOURCE_TREE: dict[str, Any] = {
    "atom_version": "atoms_v1",
    "node_count": 2,
    "depth": 2,
    "root": {"k": "const", "n": "source"},
}
_CANDIDATE_TREE: dict[str, Any] = {
    "atom_version": "atoms_v1",
    "node_count": 3,
    "depth": 3,
    "root": {"k": "const", "n": "candidate"},
}


def _theorem(code: str, **overrides: Any) -> TheoremRecord:
    payload: dict[str, Any] = {
        "proof_stripped_declaration": code,
        "statement_content_hash": hashlib.sha256(code.encode("utf-8")).hexdigest(),
        "declaration_name": "n01_fixture",
        "declaration_full_name": "n01_fixture",
    }
    payload.update(overrides)
    return theorem_record(**payload)


def _representation(
    code: str,
    *,
    atoms: tuple[str, ...],
    theorem_id: str = THM_A,
    representation_id: str = REPR_A,
    alpha: str | None = _SOURCE_ALPHA,
    tree: dict[str, Any] | None = _SOURCE_TREE,
    valid_views: bool = True,
) -> RepresentationRecord:
    statuses = dict.fromkeys(CANONICAL_VIEW_NAMES, ViewStatus.NOT_ATTEMPTED)
    for view in ("raw_proof_stripped", "headless", "signature_pp", "signature_explicit"):
        statuses[view] = ViewStatus.OK
    for view in ("semantic_atoms", "operator_tree"):
        statuses[view] = ViewStatus.OK if valid_views else ViewStatus.FAILED
    return representation_record(
        theorem_id=theorem_id,
        representation_id=representation_id,
        raw_proof_stripped=code,
        headless=code,
        signature_pp="fixture",
        signature_explicit="canonical fixture",
        semantic_atoms=atoms if valid_views else None,
        operator_tree=tree if valid_views else None,
        alpha_identity_fingerprint=alpha if valid_views else None,
        view_status=statuses,
        content_hash=hash_canonical(
            {
                "alpha": alpha,
                "atoms": atoms,
                "code": code,
                "theorem_id": theorem_id,
                "tree": tree,
                "valid_views": valid_views,
            }
        ),
    )


def _rule() -> N01OperatorRule:
    return N01OperatorRule.from_repository(
        generation_config_hash=_GENERATION_HASH,
    )


def _candidate(source: TheoremRecord, code: str, *, valid: bool = True) -> TheoremRecord:
    return _theorem(
        code,
        theorem_id=make_id("thm", {"n01_candidate": code}),
        ancestry_id=make_id("anc", {"n01_candidate": code}),
        root_ancestry_ids=source.root_ancestry_ids,
        parent_theorem_ids=(source.theorem_id,),
        elaboration_status=(
            ValidationStatus.ELABORATES_WITH_PLACEHOLDER if valid else ValidationStatus.INVALID
        ),
    )


def test_config_and_replacement_table_are_finite_versioned_and_hash_bound() -> None:
    loaded = load_n01_operator_config()

    assert loaded.config.rule_version == "1.0.0"
    assert loaded.table.table_id == "replacement_table_v1"
    assert tuple(entry.entry_id for entry in loaded.table.entries) == (
        "nat_lt_to_le",
        "nat_le_to_lt",
        "prop_and_to_or",
        "prop_or_to_and",
    )
    assert {
        (entry.source_token, entry.target_token, entry.type_precondition)
        for entry in loaded.table.entries
    } == {
        ("<", "≤", "nat_binary_order_relation"),
        ("≤", "<", "nat_binary_order_relation"),
        ("∧", "∨", "prop_binary_connective"),
        ("∨", "∧", "prop_binary_connective"),
    }
    assert len(loaded.config_hash) == 64
    assert len(loaded.table_hash) == 64


def test_replacement_entry_rejects_unbalanced_atom_mapping() -> None:
    with pytest.raises(ValidationError, match="equal length"):
        N01ReplacementEntry.model_validate(
            {
                "entry_id": "bad",
                "family_id": "n01_operator",
                "source_token": "<",
                "target_token": "≤",
                "type_precondition": "nat_binary_order_relation",
                "source_atoms": ["const:LT.lt", "const:instLTNat"],
                "target_atoms": ["const:LE.le"],
                "required_context_atoms": ["const:Nat"],
                "intended_error_types": ["E11"],
            }
        )


def test_loader_rejects_table_override_not_declared_by_config(tmp_path: Any) -> None:
    undeclared = tmp_path / "other.yaml"
    undeclared.write_text("schema_version: 1\n", encoding="utf-8")

    with pytest.raises(N01OperatorError, match="override"):
        load_n01_operator_config(table_path=undeclared)


def test_lexer_ignores_comments_strings_guillemets_and_proof() -> None:
    source = (
        "/- < /- ≤ ∨ -/ ∧ -/\n"
        'theorem «< ≤ ∧ ∨» : ("< ≤ ∧ ∨" = "< ≤ ∧ ∨") ∧ '
        "True := by /- ∧ ∨ < ≤ -/ sorry"
    )
    table = load_n01_operator_config().table

    sites = enumerate_operator_sites(source, ("const:And",), table)

    assert len(sites) == 1
    assert sites[0].entry_id == "prop_and_to_or"
    assert source[sites[0].start : sites[0].end] == "∧"


def test_nested_block_comment_depth_closes_before_following_declaration() -> None:
    source = (
        "/- outer < /- nested ∧ ∨ -/ outer ≤ -/\n"
        "theorem n01_fixture (P Q : Prop) : P ∨ Q := by sorry"
    )

    sites = enumerate_operator_sites(
        source,
        ("const:Or",),
        load_n01_operator_config().table,
    )

    assert len(sites) == 1
    assert sites[0].entry_id == "prop_or_to_and"


@pytest.mark.parametrize(
    ("code", "atoms", "target", "entry_id", "error_type"),
    [
        (
            "theorem n01_fixture (m n : Nat) : m < n := by sorry",
            ("const:LT.lt", "const:Nat", "const:instLTNat"),
            "m ≤ n",
            "nat_lt_to_le",
            "E11",
        ),
        (
            "theorem n01_fixture (m n : Nat) : m ≤ n := by sorry",
            ("const:LE.le", "const:Nat", "const:instLENat"),
            "m < n",
            "nat_le_to_lt",
            "E11",
        ),
        (
            "theorem n01_fixture (P Q : Prop) : P ∧ Q := by sorry",
            ("const:And",),
            "P ∨ Q",
            "prop_and_to_or",
            "E10",
        ),
        (
            "theorem n01_fixture (P Q : Prop) : P ∨ Q := by sorry",
            ("const:Or",),
            "P ∧ Q",
            "prop_or_to_and",
            "E10",
        ),
    ],
)
def test_each_finite_direction_is_exact_deterministic_and_invertible(
    code: str,
    atoms: tuple[str, ...],
    target: str,
    entry_id: str,
    error_type: str,
) -> None:
    source = _theorem(code)
    representation = _representation(code, atoms=atoms)
    rule = _rule()

    first = rule.generate(source, representation, 19)[0]
    replay = rule.generate(source, representation, 19)[0]

    assert first == replay
    assert target in first.candidate_code
    assert first.transformation_trace[0]["entry_id"] == entry_id
    assert first.intended_relation == IntendedRelation.NEAR_MISS
    assert first.intended_error_types == (error_type,)
    assert first.candidate_pool == "deterministic_negative_provisional"
    assert first.metadata["semantic_negative_established"] is False
    assert first.inverse_trace is not None
    assert apply_operator_trace(first.candidate_code, first.inverse_trace) == code


def test_seeded_selection_is_deterministic_and_can_reach_both_eligible_sites() -> None:
    code = "theorem n01_fixture (m n : Nat) (P : Prop) : m < n ∧ P := by sorry"
    atoms = ("const:And", "const:LT.lt", "const:Nat", "const:instLTNat")
    source = _theorem(code)
    representation = _representation(code, atoms=atoms)
    rule = _rule()
    selected: set[str] = set()

    for seed in range(64):
        first = rule.generate(source, representation, seed)[0]
        replay = rule.generate(source, representation, seed)[0]
        assert first == replay
        selected.add(str(first.transformation_trace[0]["entry_id"]))

    assert selected == {"nat_lt_to_le", "prop_and_to_or"}


@pytest.mark.parametrize(
    ("code", "atoms"),
    [
        (
            "theorem n01_fixture (x y : Real) : x < y := by sorry",
            ("const:LT.lt", "const:Real", "const:instLTReal"),
        ),
        (
            "theorem n01_fixture (a b c : Nat) : a < b ↔ b < c := by sorry",
            (
                "const:Iff",
                "const:LT.lt",
                "const:LT.lt",
                "const:Nat",
                "const:instLTNat",
                "const:instLTNat",
            ),
        ),
        (
            'theorem «< ∧ ∨» : ("<" = "<") := by sorry',
            (),
        ),
        (
            "def n01_fixture (m n : Nat) : Prop := m < n",
            ("const:LT.lt", "const:Nat", "const:instLTNat"),
        ),
    ],
)
def test_overloaded_duplicate_or_unsupported_sites_fail_closed(
    code: str,
    atoms: tuple[str, ...],
) -> None:
    applicability = _rule().assess(
        _theorem(code),
        _representation(code, atoms=atoms),
    )

    assert not applicability.applicable


def test_source_preconditions_fail_closed() -> None:
    code = "theorem n01_fixture (m n : Nat) : m < n := by sorry"
    atoms = ("const:LT.lt", "const:Nat", "const:instLTNat")
    invalid = _theorem(code, elaboration_status=ValidationStatus.INVALID)
    mismatched = _representation(code + " ", atoms=atoms)
    missing = _representation(code, atoms=atoms, valid_views=False)

    assert _rule().assess(invalid, _representation(code, atoms=atoms)).reason_codes == (
        "source_does_not_elaborate",
    )
    assert _rule().assess(_theorem(code), mismatched).reason_codes == (
        "source_representation_text_mismatch",
    )
    assert _rule().assess(_theorem(code), missing).reason_codes == ("missing_semantic_atoms",)


def test_trace_rejects_source_span_and_operation_drift() -> None:
    code = "theorem n01_fixture (m n : Nat) : m < n := by sorry"
    atoms = ("const:LT.lt", "const:Nat", "const:instLTNat")
    draft = _rule().generate(
        _theorem(code),
        _representation(code, atoms=atoms),
        3,
    )[0]

    with pytest.raises(N01OperatorError, match="expected_text"):
        apply_operator_trace(code.replace("m < n", "m  < n"), draft.transformation_trace)
    tampered = (dict(draft.transformation_trace[0], operation="unknown"),)
    with pytest.raises(N01OperatorError, match="unsupported"):
        apply_operator_trace(code, tampered)
    with pytest.raises(N01OperatorError, match="exactly_one"):
        apply_operator_trace(code, ())


def test_clean_mechanical_audit_stays_provisional_and_semantically_unresolved() -> None:
    code = "theorem n01_fixture (m n : Nat) : m < n := by sorry"
    source_atoms = ("const:LT.lt", "const:Nat", "const:instLTNat")
    candidate_atoms = ("const:LE.le", "const:Nat", "const:instLENat")
    source = _theorem(code)
    source_representation = _representation(code, atoms=source_atoms)
    rule = _rule()
    draft = rule.generate(source, source_representation, 7)[0]
    candidate = _candidate(source, draft.candidate_code)
    candidate_representation = _representation(
        draft.candidate_code,
        atoms=candidate_atoms,
        theorem_id=candidate.theorem_id,
        representation_id=make_id("repr", {"n01_candidate": draft.draft_id}),
        alpha=_CANDIDATE_ALPHA,
        tree=_CANDIDATE_TREE,
    )

    audit = rule.audit(
        source,
        source_representation,
        candidate,
        candidate_representation,
        draft,
    )

    assert audit.violation_codes == ()
    assert audit.recommended_quality_tier == QualityTier.PROVISIONAL
    assert audit.structural_diff_ok is True
    assert audit.atom_mapping_ok is True
    assert audit.inverse_or_roundtrip_ok is True
    assert audit.metadata["failed_proof_search_used"] is False
    assert audit.metadata["semantic_negative_established"] is False


@pytest.mark.parametrize(
    "failure",
    [
        "candidate_invalid",
        "identity_unchanged",
        "candidate_text_mismatch",
        "trace_tampered",
        "diff_tampered",
        "atom_delta_tampered",
    ],
)
def test_audit_quarantines_every_elaboration_or_exact_delta_violation(
    failure: str,
) -> None:
    code = "theorem n01_fixture (m n : Nat) : m < n := by sorry"
    source_atoms = ("const:LT.lt", "const:Nat", "const:instLTNat")
    candidate_atoms = (
        ("const:LT.lt", "const:Nat", "const:instLTNat")
        if failure == "atom_delta_tampered"
        else ("const:LE.le", "const:Nat", "const:instLENat")
    )
    source = _theorem(code)
    source_representation = _representation(code, atoms=source_atoms)
    rule = _rule()
    draft = rule.generate(source, source_representation, 7)[0]
    candidate = _candidate(
        source,
        draft.candidate_code,
        valid=failure != "candidate_invalid",
    )
    represented_code = (
        draft.candidate_code + " " if failure == "candidate_text_mismatch" else draft.candidate_code
    )
    candidate_representation = _representation(
        represented_code,
        atoms=candidate_atoms,
        theorem_id=candidate.theorem_id,
        representation_id=make_id("repr", {"n01_failure": failure}),
        alpha=(_SOURCE_ALPHA if failure == "identity_unchanged" else _CANDIDATE_ALPHA),
        tree=_CANDIDATE_TREE,
    )
    if failure == "trace_tampered":
        step = dict(draft.transformation_trace[0])
        step["replacement_table_hash"] = "0" * 64
        draft = draft.model_copy(update={"transformation_trace": (step,)})
    elif failure == "diff_tampered":
        diff = dict(draft.expected_structural_diff)
        diff["source_span_start"] = 999
        draft = draft.model_copy(update={"expected_structural_diff": diff})

    audit = rule.audit(
        source,
        source_representation,
        candidate,
        candidate_representation,
        draft,
    )

    assert audit.violation_codes
    assert audit.recommended_validation_status == ValidationStatus.QUARANTINED
    assert audit.recommended_quality_tier == QualityTier.UNKNOWN
    assert audit.metadata["failed_proof_search_used"] is False
    assert audit.metadata["semantic_negative_established"] is False

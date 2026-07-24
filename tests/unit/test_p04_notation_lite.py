"""LF-017 P04-lite exact notation/direct-form rewrites."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from leanfaith.config.hashing import hash_canonical
from leanfaith.schemas import (
    CANONICAL_VIEW_NAMES,
    QualityTier,
    ValidationStatus,
    ViewStatus,
    make_id,
)
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms import TransformationRegistry, load_transformation_registry
from leanfaith.transforms.positives.p04_notation_lite import (
    P04NotationError,
    P04NotationLiteRule,
    apply_notation_trace,
    enumerate_notation_sites,
    load_p04_notation_config,
    notation_table_hash,
)
from tests.unit.record_factories import (
    REPR_A,
    THM_A,
    representation_record,
    theorem_record,
)

_REGISTRY_HASH = "c" * 64
_ALPHA = "d" * 64
_TREE: dict[str, Any] = {
    "atom_version": "atoms_v1",
    "node_count": 1,
    "depth": 1,
    "root": {"k": "const", "n": "fixture"},
}


def _theorem(code: str, **overrides: Any) -> TheoremRecord:
    payload: dict[str, Any] = {
        "proof_stripped_declaration": code,
        "statement_content_hash": hashlib.sha256(code.encode("utf-8")).hexdigest(),
        "declaration_name": "p04_fixture",
        "declaration_full_name": "p04_fixture",
    }
    payload.update(overrides)
    return theorem_record(**payload)


def _representation(
    code: str,
    *,
    constants: tuple[str, ...] = ("Nat",),
    theorem_id: str = THM_A,
    representation_id: str = REPR_A,
    alpha: str | None = _ALPHA,
    tree: dict[str, Any] | None = _TREE,
    valid_views: bool = True,
) -> RepresentationRecord:
    statuses = dict.fromkeys(CANONICAL_VIEW_NAMES, ViewStatus.NOT_ATTEMPTED)
    statuses["raw_proof_stripped"] = ViewStatus.OK
    statuses["headless"] = ViewStatus.OK
    statuses["signature_pp"] = ViewStatus.OK
    statuses["signature_explicit"] = ViewStatus.OK if valid_views else ViewStatus.FAILED
    statuses["semantic_atoms"] = ViewStatus.OK if valid_views else ViewStatus.FAILED
    statuses["operator_tree"] = ViewStatus.OK if valid_views else ViewStatus.FAILED
    return representation_record(
        theorem_id=theorem_id,
        representation_id=representation_id,
        raw_proof_stripped=code,
        headless=code,
        signature_pp="fixture",
        signature_explicit="canonical fixture" if valid_views else None,
        semantic_atoms=(
            tuple(f"const:{constant}" for constant in constants) if valid_views else None
        ),
        operator_tree=tree if valid_views else None,
        alpha_identity_fingerprint=alpha if valid_views else None,
        view_status=statuses,
        content_hash=hash_canonical(
            {
                "code": code,
                "constants": constants,
                "theorem_id": theorem_id,
                "valid_views": valid_views,
            }
        ),
    )


def _rule() -> P04NotationLiteRule:
    return P04NotationLiteRule.from_repository(
        generation_config_hash=_REGISTRY_HASH,
    )


def _candidate(source: TheoremRecord, code: str, *, valid: bool = True) -> TheoremRecord:
    return _theorem(
        code,
        theorem_id=make_id("thm", {"p04_candidate": code}),
        ancestry_id=make_id("anc", {"p04_candidate": code}),
        root_ancestry_ids=source.root_ancestry_ids,
        parent_theorem_ids=(source.theorem_id,),
        elaboration_status=(
            ValidationStatus.ELABORATES_WITH_PLACEHOLDER if valid else ValidationStatus.INVALID
        ),
    )


def test_config_is_finite_versioned_and_table_hash_is_stable() -> None:
    loaded = load_p04_notation_config()

    assert loaded.config.rule_version == "1.0.0"
    assert tuple(entry.entry_id for entry in loaded.config.entries) == (
        "int_type",
        "nat_type",
    )
    assert {
        (entry.notation, entry.direct_form, entry.elaborated_constant)
        for entry in loaded.config.entries
    } == {("ℕ", "Nat", "Nat"), ("ℤ", "Int", "Int")}
    assert notation_table_hash(loaded.config) == notation_table_hash(loaded.config)
    assert len(notation_table_hash(loaded.config)) == 64


def test_lexer_ignores_nested_comments_strings_guillemets_and_partial_identifiers() -> None:
    source = (
        "/- Nat /- ℕ ℤ -/ Int -/\n"
        "theorem «Nat ℕ Int ℤ» (x : Nat) : "
        '("Nat \\"ℕ\\" Int ℤ" = "Nat") ∧ '
        "Natural = Natural ∧ fooNat = fooNat ∧ ℕfoo = ℕfoo ∧ "
        "Nat.succ x = x := by sorry"
    )

    sites = enumerate_notation_sites(source, load_p04_notation_config().config)

    assert len(sites) == 1
    assert sites[0].source_token == "Nat"
    assert source[sites[0].start : sites[0].end] == "Nat"
    assert "x : Nat" in source[sites[0].start - 4 : sites[0].end + 1]


@pytest.mark.parametrize(
    ("source_token", "target_token", "constant", "direction"),
    [
        ("Nat", "ℕ", "Nat", "direct_to_notation"),
        ("ℕ", "Nat", "Nat", "notation_to_direct"),
        ("Int", "ℤ", "Int", "direct_to_notation"),
        ("ℤ", "Int", "Int", "notation_to_direct"),
    ],
)
def test_each_approved_direction_is_exact_and_roundtrips(
    source_token: str,
    target_token: str,
    constant: str,
    direction: str,
) -> None:
    code = f"theorem p04_fixture (x : {source_token}) : x = x := by sorry"
    theorem = _theorem(code)
    representation = _representation(code, constants=(constant,))
    draft = _rule().generate(theorem, representation, 19)[0]

    assert f"(x : {target_token})" in draft.candidate_code
    assert draft.transformation_trace[0]["direction"] == direction
    assert draft.transformation_trace[0]["table_hash"] == _rule().table_hash
    token_index = draft.transformation_trace[0]["token_index"]
    assert isinstance(token_index, int) and token_index >= 0
    assert draft.inverse_trace is not None
    assert (
        apply_notation_trace(
            draft.candidate_code,
            draft.inverse_trace,
            expected_table_hash=_rule().table_hash,
        )
        == code
    )


def test_seed_selection_is_deterministic_and_reaches_distinct_sites() -> None:
    code = (
        "theorem p04_fixture (n : Nat) (m : ℕ) (i : Int) (j : ℤ) "
        ": n = n ∧ m = m ∧ i = i ∧ j = j := by sorry"
    )
    theorem = _theorem(code)
    representation = _representation(code, constants=("Int", "Nat"))
    rule = _rule()
    seen: set[tuple[object, ...]] = set()

    for seed in range(64):
        first = rule.generate(theorem, representation, seed)[0]
        replay = rule.generate(theorem, representation, seed)[0]
        assert first == replay
        trace = first.transformation_trace[0]
        seen.add((trace["entry_id"], trace["direction"], trace["start"]))

    assert len(seen) >= 3


@pytest.mark.parametrize(
    "source",
    [
        'theorem «Nat ℕ» : ("Nat ℕ" = "Nat ℕ") := by sorry',
        "theorem p04_fixture : Natural = Natural := by sorry",
        "theorem p04_fixture : Nat.succ 0 = Nat.succ 0 := by sorry",
        "theorem p04_fixture : True := by /- Nat /- ℕ -/ -/ sorry",
    ],
)
def test_no_exact_supported_signature_site_is_not_applicable(source: str) -> None:
    theorem = _theorem(source)
    representation = _representation(source, constants=("Nat",))

    applicability = _rule().assess(theorem, representation)

    assert not applicability.applicable


def test_trace_rejects_input_span_token_and_table_drift() -> None:
    code = "theorem p04_fixture (x : Nat) : x = x := by sorry"
    draft = _rule().generate(_theorem(code), _representation(code), 3)[0]
    trace = draft.transformation_trace

    with pytest.raises(P04NotationError, match="input_code_hash"):
        apply_notation_trace(code.replace("x = x", "x =  x"), trace)
    with pytest.raises(P04NotationError, match="table_hash"):
        apply_notation_trace(code, trace, expected_table_hash="0" * 64)
    tampered = (dict(trace[0], token_hash="0" * 64),)
    with pytest.raises(P04NotationError, match="token_hash"):
        apply_notation_trace(code, tampered)


def test_source_preconditions_fail_closed() -> None:
    code = "theorem p04_fixture (x : Nat) : x = x := by sorry"
    invalid = _theorem(code, elaboration_status=ValidationStatus.INVALID)
    mismatched = _representation(code + " ")

    assert _rule().assess(invalid, _representation(code)).reason_codes == (
        "source_does_not_elaborate",
    )
    assert _rule().assess(_theorem(code), mismatched).reason_codes == (
        "source_representation_mismatch",
    )


def test_exact_identity_audit_is_provisional_only() -> None:
    code = "theorem p04_fixture (x : Nat) : x = x := by sorry"
    source = _theorem(code)
    source_representation = _representation(code)
    rule = _rule()
    draft = rule.generate(source, source_representation, 7)[0]
    candidate = _candidate(source, draft.candidate_code)
    candidate_representation = _representation(
        draft.candidate_code,
        theorem_id=candidate.theorem_id,
        representation_id=make_id("repr", {"p04_candidate": draft.draft_id}),
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
    assert audit.metadata["elaborated_identity_exact"] is True
    assert audit.metadata["operator_tree_equal"] is True


def test_audit_rejects_elaborated_identity_drift() -> None:
    code = "theorem p04_fixture (x : Nat) : x = x := by sorry"
    source = _theorem(code)
    source_representation = _representation(code)
    rule = _rule()
    draft = rule.generate(source, source_representation, 7)[0]
    candidate = _candidate(source, draft.candidate_code)
    candidate_representation = _representation(
        draft.candidate_code,
        theorem_id=candidate.theorem_id,
        representation_id=make_id("repr", {"p04_bad": draft.draft_id}),
        tree={**_TREE, "node_count": 2},
        alpha="e" * 64,
    )

    audit = rule.audit(
        source,
        source_representation,
        candidate,
        candidate_representation,
        draft,
    )

    assert audit.recommended_quality_tier == QualityTier.UNKNOWN
    assert audit.recommended_validation_status == ValidationStatus.QUARANTINED
    assert "alpha_identity_mismatch" in audit.violation_codes
    assert "operator_tree_mismatch" in audit.violation_codes


def test_unavailable_target_notation_is_rejected_not_promoted() -> None:
    code = "theorem p04_fixture (x : Nat) : x = x := by sorry"
    source = _theorem(code)
    source_representation = _representation(code)
    rule = _rule()
    draft = rule.generate(source, source_representation, 7)[0]
    assert draft.transformation_trace[0]["direction"] == "direct_to_notation"
    candidate = _candidate(source, draft.candidate_code, valid=False)
    candidate_representation = _representation(
        draft.candidate_code,
        theorem_id=candidate.theorem_id,
        representation_id=make_id("repr", {"p04_unavailable": draft.draft_id}),
        valid_views=False,
    )

    audit = rule.audit(
        source,
        source_representation,
        candidate,
        candidate_representation,
        draft,
    )

    assert audit.recommended_quality_tier == QualityTier.UNKNOWN
    assert audit.recommended_validation_status == ValidationStatus.QUARANTINED
    assert "candidate_not_elaborated" in audit.violation_codes
    assert "target_notation_unavailable_or_invalid" in audit.violation_codes


@pytest.mark.parametrize(
    "failure",
    [
        "source_invalid",
        "candidate_representation_text",
        "trace_metadata",
        "expected_diff",
        "atom_mapping",
        "representation_lineage",
    ],
)
def test_audit_quarantines_lineage_and_certificate_tampering(failure: str) -> None:
    code = "theorem p04_fixture (x : Nat) : x = x := by sorry"
    source = _theorem(
        code,
        elaboration_status=(
            ValidationStatus.INVALID
            if failure == "source_invalid"
            else ValidationStatus.ELABORATES_WITH_PLACEHOLDER
        ),
    )
    source_representation = _representation(code)
    rule = _rule()
    # Generation correctly rejects an invalid source; create its draft from the
    # corresponding valid record solely to exercise the fail-closed audit.
    generation_source = _theorem(code) if failure == "source_invalid" else source
    draft = rule.generate(generation_source, source_representation, 7)[0]
    candidate = _candidate(source, draft.candidate_code)
    candidate_representation = _representation(
        (
            draft.candidate_code + " "
            if failure == "candidate_representation_text"
            else draft.candidate_code
        ),
        theorem_id=(THM_A if failure == "representation_lineage" else candidate.theorem_id),
        representation_id=make_id("repr", {"p04_tamper": failure}),
    )
    if failure == "trace_metadata":
        step = dict(draft.transformation_trace[0])
        step["entry_id"] = "forged_entry"
        draft = draft.model_copy(update={"transformation_trace": (step,)})
    elif failure == "expected_diff":
        expected = dict(draft.expected_structural_diff)
        expected["source_span_start"] = 999
        draft = draft.model_copy(update={"expected_structural_diff": expected})
    elif failure == "atom_mapping":
        draft = draft.model_copy(update={"expected_atom_mapping": {"Nat": "Int"}})

    audit = rule.audit(
        source,
        source_representation,
        candidate,
        candidate_representation,
        draft,
    )

    assert audit.violation_codes
    assert audit.recommended_quality_tier == QualityTier.UNKNOWN
    assert audit.recommended_validation_status == ValidationStatus.QUARANTINED


def test_repository_registry_dispatches_available_p04() -> None:
    loaded = load_transformation_registry()
    runtime = TransformationRegistry(loaded)
    runtime.register(
        P04NotationLiteRule.from_repository(
            generation_config_hash=loaded.registry_hash,
        )
    )
    code = "theorem p04_fixture (x : Nat) : x = x := by sorry"

    execution = runtime.execute(
        "p04_notation_lite",
        _theorem(code),
        _representation(code),
        11,
    )

    assert execution.attempt.terminal_outcome == "generated"
    assert execution.drafts[0].family_id == "p04_notation_lite"

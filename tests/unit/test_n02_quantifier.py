"""LF-018 N02 exact quantifier-mutation tests."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

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
from leanfaith.transforms.negatives.n02_quantifier import (
    N02QuantifierError,
    N02QuantifierRule,
    apply_quantifier_trace,
    enumerate_quantifier_sites,
    load_n02_quantifier_config,
    quantifier_table_hash,
)
from tests.unit.record_factories import REPR_A, THM_A, representation_record, theorem_record

_REGISTRY_HASH = "4" * 64
_SOURCE_ALPHA = "5" * 64
_CANDIDATE_ALPHA = "6" * 64
_SOURCE_TREE: dict[str, Any] = {
    "atom_version": "atoms_v1",
    "node_count": 2,
    "depth": 2,
    "root": {"k": "forall", "dom": {"k": "const", "n": "Nat"}},
}
_CANDIDATE_TREE: dict[str, Any] = {
    "atom_version": "atoms_v1",
    "node_count": 3,
    "depth": 3,
    "root": {"k": "app", "fn": {"k": "const", "n": "Exists"}},
}


def _theorem(code: str, **overrides: Any) -> TheoremRecord:
    payload: dict[str, Any] = {
        "proof_stripped_declaration": code,
        "statement_content_hash": hashlib.sha256(code.encode("utf-8")).hexdigest(),
        "declaration_name": "n02_fixture",
        "declaration_full_name": "n02_fixture",
    }
    payload.update(overrides)
    return theorem_record(**payload)


def _representation(
    code: str,
    *,
    theorem_id: str = THM_A,
    representation_id: str = REPR_A,
    alpha: str | None = _SOURCE_ALPHA,
    tree: dict[str, Any] | None = _SOURCE_TREE,
    valid_views: bool = True,
) -> RepresentationRecord:
    statuses = dict.fromkeys(CANONICAL_VIEW_NAMES, ViewStatus.NOT_ATTEMPTED)
    for view in ("raw_proof_stripped", "headless"):
        statuses[view] = ViewStatus.OK
    for view in ("signature_pp", "signature_explicit", "semantic_atoms", "operator_tree"):
        statuses[view] = ViewStatus.OK if valid_views else ViewStatus.FAILED
    return representation_record(
        theorem_id=theorem_id,
        representation_id=representation_id,
        raw_proof_stripped=code,
        headless=code,
        signature_pp="fixture" if valid_views else None,
        signature_explicit="canonical fixture" if valid_views else None,
        semantic_atoms=("const:Nat",) if valid_views else None,
        operator_tree=tree if valid_views else None,
        alpha_identity_fingerprint=alpha if valid_views else None,
        view_status=statuses,
        content_hash=hash_canonical(
            {
                "alpha": alpha,
                "code": code,
                "theorem_id": theorem_id,
                "tree": tree,
                "valid_views": valid_views,
            }
        ),
    )


def _rule() -> N02QuantifierRule:
    return N02QuantifierRule.from_repository(registry_hash=_REGISTRY_HASH)


def _candidate(source: TheoremRecord, code: str, *, valid: bool = True) -> TheoremRecord:
    return _theorem(
        code,
        theorem_id=make_id("thm", {"n02_candidate": code}),
        ancestry_id=make_id("anc", {"n02_candidate": code}),
        root_ancestry_ids=source.root_ancestry_ids,
        parent_theorem_ids=(source.theorem_id,),
        elaboration_status=(
            ValidationStatus.ELABORATES_WITH_PLACEHOLDER if valid else ValidationStatus.INVALID
        ),
    )


def test_config_is_finite_invertible_and_hash_stable() -> None:
    loaded = load_n02_quantifier_config()

    assert loaded.config.rule_version == "1.0.0"
    assert {(item.source_token, item.target_token) for item in loaded.config.mutations} == {
        ("∀", "∃"),
        ("∃", "∀"),
    }
    assert quantifier_table_hash(loaded.config) == quantifier_table_hash(loaded.config)
    assert len(quantifier_table_hash(loaded.config)) == 64


def test_enumerator_ignores_comments_strings_guillemets_and_proof() -> None:
    source = (
        "/- ∀ /- ∃ -/ -/\n"
        'theorem «∀ ∃» : ("∀ ∃" = "∀ ∃") ∧ '
        "(∀ x : Nat, x = x) := by /- ∀ ∃ -/ sorry"
    )

    sites = enumerate_quantifier_sites(source, load_n02_quantifier_config().config)

    assert len(sites) == 1
    assert sites[0].source_token == "∀"
    assert source[sites[0].start : sites[0].end] == "∀"


@pytest.mark.parametrize(
    ("source_token", "target_token"),
    [("∀", "∃"), ("∃", "∀")],
)
def test_each_direction_is_deterministic_and_exactly_invertible(
    source_token: str,
    target_token: str,
) -> None:
    code = f"theorem n02_fixture : ({source_token} x : Nat, x = x) := by sorry"
    source = _theorem(code)
    source_representation = _representation(code)
    rule = _rule()

    first = rule.generate(source, source_representation, 19)[0]
    replay = rule.generate(source, source_representation, 19)[0]

    assert first == replay
    assert f"({target_token} x : Nat, x = x)" in first.candidate_code
    assert first.intended_relation == IntendedRelation.NEAR_MISS
    assert first.intended_error_types == ("E04", "E05")
    assert first.inverse_trace is not None
    assert (
        apply_quantifier_trace(
            first.candidate_code,
            first.inverse_trace,
            expected_table_hash=rule.table_hash,
        )
        == code
    )


def test_seeded_selection_reaches_distinct_sites() -> None:
    code = "theorem n02_fixture : (∀ x : Nat, x = x) ∧ (∃ y : Nat, y = y) := by sorry"
    rule = _rule()
    source = _theorem(code)
    representation = _representation(code)
    selected: set[tuple[object, ...]] = set()

    for seed in range(64):
        draft = rule.generate(source, representation, seed)[0]
        step = draft.transformation_trace[0]
        selected.add((step["start"], step["mutation_id"]))

    assert len(selected) == 2


@pytest.mark.parametrize(
    "source",
    [
        'theorem «∀ ∃» : ("∀" = "∀") := by sorry',
        "theorem n02_fixture : True := by sorry",
        "def n02_fixture : Prop := ∀ x : Nat, x = x",
    ],
)
def test_unsupported_or_absent_site_is_not_applicable(source: str) -> None:
    applicability = _rule().assess(_theorem(source), _representation(source))

    assert not applicability.applicable


def test_source_preconditions_fail_closed() -> None:
    code = "theorem n02_fixture : (∀ x : Nat, x = x) := by sorry"
    invalid = _theorem(code, elaboration_status=ValidationStatus.INVALID)
    mismatched = _representation(code + " ")
    missing = _representation(code, valid_views=False)

    assert _rule().assess(invalid, _representation(code)).reason_codes == (
        "source_does_not_elaborate",
    )
    assert _rule().assess(_theorem(code), mismatched).reason_codes == (
        "source_representation_text_mismatch",
    )
    assert _rule().assess(_theorem(code), missing).reason_codes == ("source_required_view_missing",)


def test_trace_rejects_source_table_and_token_drift() -> None:
    code = "theorem n02_fixture : (∀ x : Nat, x = x) := by sorry"
    draft = _rule().generate(_theorem(code), _representation(code), 3)[0]

    with pytest.raises(N02QuantifierError, match="input_code_hash"):
        apply_quantifier_trace(code.replace("x = x", "x =  x"), draft.transformation_trace)
    with pytest.raises(N02QuantifierError, match="table_hash"):
        apply_quantifier_trace(
            code,
            draft.transformation_trace,
            expected_table_hash="0" * 64,
        )
    tampered = (dict(draft.transformation_trace[0], token_hash="0" * 64),)
    with pytest.raises(N02QuantifierError, match="token_hash"):
        apply_quantifier_trace(code, tampered)


def test_clean_mechanical_audit_remains_provisional_and_unresolved() -> None:
    code = "theorem n02_fixture : (∀ x : Nat, x = x) := by sorry"
    source = _theorem(code)
    source_representation = _representation(code)
    rule = _rule()
    draft = rule.generate(source, source_representation, 7)[0]
    candidate = _candidate(source, draft.candidate_code)
    candidate_representation = _representation(
        draft.candidate_code,
        theorem_id=candidate.theorem_id,
        representation_id=make_id("repr", {"n02_candidate": draft.draft_id}),
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
    assert audit.inverse_or_roundtrip_ok is True
    assert audit.metadata["semantic_negative_resolved"] is False


@pytest.mark.parametrize(
    "failure",
    [
        "candidate_invalid",
        "identity_unchanged",
        "candidate_text_mismatch",
        "trace_tampered",
        "diff_tampered",
    ],
)
def test_audit_quarantines_every_certificate_or_elaboration_violation(
    failure: str,
) -> None:
    code = "theorem n02_fixture : (∀ x : Nat, x = x) := by sorry"
    source = _theorem(code)
    source_representation = _representation(code)
    rule = _rule()
    draft = rule.generate(source, source_representation, 7)[0]
    candidate = _candidate(
        source,
        draft.candidate_code,
        valid=failure != "candidate_invalid",
    )
    candidate_code = (
        draft.candidate_code + " " if failure == "candidate_text_mismatch" else draft.candidate_code
    )
    candidate_representation = _representation(
        candidate_code,
        theorem_id=candidate.theorem_id,
        representation_id=make_id("repr", {"n02_failure": failure}),
        alpha=(_SOURCE_ALPHA if failure == "identity_unchanged" else _CANDIDATE_ALPHA),
        tree=_SOURCE_TREE if failure == "identity_unchanged" else _CANDIDATE_TREE,
    )
    if failure == "trace_tampered":
        step = dict(draft.transformation_trace[0])
        step["token_hash"] = "0" * 64
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

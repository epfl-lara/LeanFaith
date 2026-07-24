"""LF-018 N07 finite numeric-bound mutation tests."""

from __future__ import annotations

import hashlib
from collections import Counter
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
from leanfaith.transforms.negatives.n07_literal_bound import (
    N07LiteralBoundError,
    N07LiteralBoundRule,
    apply_literal_bound_trace,
    enumerate_literal_bound_sites,
    literal_bound_table_hash,
    load_n07_literal_bound_config,
)
from tests.unit.record_factories import REPR_A, THM_A, representation_record, theorem_record

_REGISTRY_HASH = "7" * 64
_SOURCE_ALPHA = "8" * 64
_CANDIDATE_ALPHA = "9" * 64


def _tree(literal: str) -> dict[str, Any]:
    return {
        "atom_version": "atoms_v1",
        "node_count": 5,
        "depth": 4,
        "root": {
            "k": "app",
            "fn": {"k": "const", "n": "LE.le"},
            "arg": {"k": "lit", "nat": int(literal)},
        },
    }


def _atoms(literal: str) -> tuple[str, ...]:
    return ("const:LE.le", "const:Nat", f"lit:nat:{literal}")


def _theorem(code: str, **overrides: Any) -> TheoremRecord:
    payload: dict[str, Any] = {
        "proof_stripped_declaration": code,
        "statement_content_hash": hashlib.sha256(code.encode("utf-8")).hexdigest(),
        "declaration_name": "n07_fixture",
        "declaration_full_name": "n07_fixture",
    }
    payload.update(overrides)
    return theorem_record(**payload)


def _extraction_style_statement_hash(code: str) -> str:
    """Mirror extraction's signature hash, which intentionally excludes the proof."""

    signature = code.removesuffix(" := by sorry")
    assert signature != code
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()


def _representation(
    code: str,
    *,
    literal: str = "0",
    theorem_id: str = THM_A,
    representation_id: str = REPR_A,
    alpha: str | None = _SOURCE_ALPHA,
    semantic_atoms: tuple[str, ...] | None = None,
    tree: dict[str, Any] | None = None,
    valid_views: bool = True,
) -> RepresentationRecord:
    statuses = dict.fromkeys(CANONICAL_VIEW_NAMES, ViewStatus.NOT_ATTEMPTED)
    for view in ("raw_proof_stripped", "headless"):
        statuses[view] = ViewStatus.OK
    for view in ("signature_pp", "signature_explicit", "semantic_atoms", "operator_tree"):
        statuses[view] = ViewStatus.OK if valid_views else ViewStatus.FAILED
    actual_atoms = semantic_atoms if semantic_atoms is not None else _atoms(literal)
    actual_tree = tree if tree is not None else _tree(literal)
    return representation_record(
        theorem_id=theorem_id,
        representation_id=representation_id,
        raw_proof_stripped=code,
        headless=code,
        signature_pp=f"fixture {literal}" if valid_views else None,
        signature_explicit=f"canonical fixture {literal}" if valid_views else None,
        semantic_atoms=actual_atoms if valid_views else None,
        operator_tree=actual_tree if valid_views else None,
        alpha_identity_fingerprint=alpha if valid_views else None,
        view_status=statuses,
        content_hash=hash_canonical(
            {
                "alpha": alpha,
                "atoms": actual_atoms,
                "code": code,
                "theorem_id": theorem_id,
                "tree": actual_tree,
                "valid_views": valid_views,
            }
        ),
    )


def _rule() -> N07LiteralBoundRule:
    return N07LiteralBoundRule.from_repository(registry_hash=_REGISTRY_HASH)


def _candidate(source: TheoremRecord, code: str, *, valid: bool = True) -> TheoremRecord:
    return _theorem(
        code,
        theorem_id=make_id("thm", {"n07_candidate": code}),
        ancestry_id=make_id("anc", {"n07_candidate": code}),
        root_ancestry_ids=source.root_ancestry_ids,
        parent_theorem_ids=(source.theorem_id,),
        elaboration_status=(
            ValidationStatus.ELABORATES_WITH_PLACEHOLDER if valid else ValidationStatus.INVALID
        ),
    )


def test_config_is_exact_finite_invertible_and_hash_stable() -> None:
    loaded = load_n07_literal_bound_config()

    assert loaded.config.rule_version == "1.0.0"
    assert loaded.config.failed_proof_search_is_negative_evidence is False
    assert loaded.config.comparison_operators == ("<", ">", "≤", "≥")
    assert {(item.source_literal, item.target_literal) for item in loaded.config.mutations} == {
        ("0", "1"),
        ("1", "0"),
    }
    assert {error for item in loaded.config.mutations for error in item.intended_error_types} == {
        "E17"
    }
    table_hash = literal_bound_table_hash(loaded.config)
    assert table_hash == literal_bound_table_hash(loaded.config)
    assert len(table_hash) == 64


def test_enumerator_ignores_comments_strings_characters_guillemets_and_proof() -> None:
    source = (
        "/- 0 ≤ 1 /- 1 < 0 -/ -/\n"
        "theorem «0 ≤ 1» (n : Nat) : "
        "(\"0 ≤ 1\" = \"0 ≤ 1\") ∧ ('0' = '0') ∧ (n ≤ 0) "
        ":= by /- 1 < n -/ sorry"
    )

    sites = enumerate_literal_bound_sites(
        source,
        load_n07_literal_bound_config().config,
    )

    assert len(sites) == 1
    assert sites[0].source_literal == "0"
    assert sites[0].comparison_operator == "≤"
    assert sites[0].operand_side == "right"
    assert source[sites[0].start : sites[0].end] == "0"


@pytest.mark.parametrize("operator", ["<", ">", "≤", "≥"])
@pytest.mark.parametrize(
    ("side", "source_literal", "target_literal"),
    [
        ("left", "0", "1"),
        ("left", "1", "0"),
        ("right", "0", "1"),
        ("right", "1", "0"),
    ],
)
def test_operator_side_direction_matrix_is_exact_and_invertible(
    operator: str,
    side: str,
    source_literal: str,
    target_literal: str,
) -> None:
    proposition = (
        f"{source_literal} {operator} n" if side == "left" else f"n {operator} {source_literal}"
    )
    code = f"theorem n07_fixture (n : Nat) : {proposition} := by sorry"
    source = _theorem(code)
    representation = _representation(code, literal=source_literal)
    rule = _rule()

    first = rule.generate(source, representation, 29)[0]
    replay = rule.generate(source, representation, 29)[0]

    assert first == replay
    assert first.intended_relation == IntendedRelation.NEAR_MISS
    assert first.intended_error_types == ("E17",)
    assert first.metadata["failed_proof_search_used"] is False
    assert first.metadata["semantic_negative_resolved"] is False
    assert first.inverse_trace is not None
    assert first.transformation_trace[0]["operand_side"] == side
    assert first.transformation_trace[0]["comparison_operator"] == operator
    assert (
        apply_literal_bound_trace(
            first.candidate_code,
            first.inverse_trace,
            expected_table_hash=rule.table_hash,
        )
        == code
    )
    assert first.candidate_code.count(target_literal) >= 1


@pytest.mark.parametrize(
    "source",
    [
        "theorem n07_fixture (n : Nat) : n + 1 ≤ n := by sorry",
        "theorem n07_fixture (n : Nat) : n ≤ 1 + 1 := by sorry",
        "theorem n07_fixture (f : Nat → Nat) (n : Nat) : f 1 < n := by sorry",
        "theorem n07_fixture (n : Nat) : n ≤ 2 := by sorry",
        "theorem n07_fixture (n : Nat) : n ≤ 10 := by sorry",
        "theorem n07_fixture (n : Nat) : n ≤ (1) := by sorry",
        "theorem n07_fixture (n : Nat) : n = 1 := by sorry",
        "theorem n07_fixture (n : Nat) : n ≤ 0x10 := by sorry",
        "def n07_fixture : Prop := 0 < 1",
    ],
)
def test_out_of_scope_numeric_or_non_theorem_sites_are_not_applicable(source: str) -> None:
    literal = "1" if "1" in source else "0"
    applicability = _rule().assess(
        _theorem(source),
        _representation(source, literal=literal),
    )

    assert not applicability.applicable


def test_seeded_selection_reaches_each_of_multiple_sites() -> None:
    code = "theorem n07_fixture (n : Nat) : (0 < n) ∧ (n ≤ 1) := by sorry"
    source = _theorem(code)
    representation = _representation(
        code,
        semantic_atoms=(*_atoms("0"), "lit:nat:1"),
    )
    selected: set[tuple[object, ...]] = set()

    for seed in range(128):
        draft = _rule().generate(source, representation, seed)[0]
        step = draft.transformation_trace[0]
        selected.add((step["start"], step["mutation_id"]))
        assert (
            apply_literal_bound_trace(
                draft.candidate_code,
                draft.inverse_trace or (),
                expected_table_hash=_rule().table_hash,
            )
            == code
        )

    assert len(selected) == 2


def test_applicability_canonicalizes_two_short_token_indices() -> None:
    code = "theorem t : 0 < 1 := by sorry"
    representation = _representation(
        code,
        semantic_atoms=("lit:nat:0", "const:LT.lt", "lit:nat:1"),
    )

    applicability = _rule().assess(_theorem(code), representation)

    assert applicability.applicable
    assert len(applicability.matched_nodes) == 2
    assert applicability.matched_nodes == tuple(sorted(applicability.matched_nodes))


def test_source_preconditions_fail_closed() -> None:
    code = "theorem n07_fixture (n : Nat) : n ≤ 0 := by sorry"
    invalid = _theorem(code, elaboration_status=ValidationStatus.INVALID)
    mismatched = _representation(code + " ")
    missing = _representation(code, valid_views=False)
    wrong_lineage = _representation(
        code,
        theorem_id=make_id("thm", {"wrong": "lineage"}),
    )

    assert _rule().assess(invalid, _representation(code)).reason_codes == (
        "source_does_not_elaborate",
    )
    assert _rule().assess(_theorem(code), mismatched).reason_codes == (
        "source_representation_text_mismatch",
    )
    assert _rule().assess(_theorem(code), missing).reason_codes == ("source_required_view_missing",)
    assert _rule().assess(_theorem(code), wrong_lineage).reason_codes == (
        "source_representation_lineage_mismatch",
    )


def test_trace_rejects_source_table_literal_and_operation_drift() -> None:
    code = "theorem n07_fixture (n : Nat) : n ≤ 0 := by sorry"
    draft = _rule().generate(_theorem(code), _representation(code), 3)[0]

    with pytest.raises(N07LiteralBoundError, match="input_code_hash"):
        apply_literal_bound_trace(code.replace("n ≤ 0", "n  ≤ 0"), draft.transformation_trace)
    with pytest.raises(N07LiteralBoundError, match="table_hash"):
        apply_literal_bound_trace(
            code,
            draft.transformation_trace,
            expected_table_hash="0" * 64,
        )
    tampered_hash = (dict(draft.transformation_trace[0], literal_hash="0" * 64),)
    with pytest.raises(N07LiteralBoundError, match="literal_hash"):
        apply_literal_bound_trace(code, tampered_hash)
    tampered_operation = (dict(draft.transformation_trace[0], operation="swap_arguments"),)
    with pytest.raises(N07LiteralBoundError, match="operation"):
        apply_literal_bound_trace(code, tampered_operation)
    for field_name, value in (
        ("token_index", draft.transformation_trace[0]["token_index"] + 1),
        ("comparison_operator", "<"),
        ("operand_side", "left"),
    ):
        tampered_surface = (dict(draft.transformation_trace[0], **{field_name: value}),)
        with pytest.raises(N07LiteralBoundError, match="surface_site"):
            apply_literal_bound_trace(code, tampered_surface)
    tampered_mutation = (dict(draft.transformation_trace[0], mutation_id="one_to_zero"),)
    with pytest.raises(N07LiteralBoundError, match="malformed"):
        apply_literal_bound_trace(code, tampered_mutation)


def _clean_audit_fixture() -> tuple[
    N07LiteralBoundRule,
    TheoremRecord,
    RepresentationRecord,
    TheoremRecord,
    RepresentationRecord,
    Any,
]:
    code = "theorem n07_fixture (n : Nat) : n ≤ 0 := by sorry"
    source = _theorem(
        code,
        statement_content_hash=_extraction_style_statement_hash(code),
    )
    source_representation = _representation(code, literal="0")
    rule = _rule()
    draft = rule.generate(source, source_representation, 7)[0]
    candidate = _candidate(source, draft.candidate_code)
    candidate_representation = _representation(
        draft.candidate_code,
        literal="1",
        theorem_id=candidate.theorem_id,
        representation_id=make_id("repr", {"n07_candidate": draft.draft_id}),
        alpha=_CANDIDATE_ALPHA,
    )
    return (
        rule,
        source,
        source_representation,
        candidate,
        candidate_representation,
        draft,
    )


def test_clean_audit_accepts_extraction_style_source_hash_and_remains_unresolved() -> None:
    rule, source, source_representation, candidate, candidate_representation, draft = (
        _clean_audit_fixture()
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
    assert audit.metadata["semantic_negative_resolved"] is False
    assert audit.metadata["failed_proof_search_used"] is False


def test_audit_quarantines_invalid_candidate_and_never_resolves_negative() -> None:
    rule, source, source_representation, candidate, candidate_representation, draft = (
        _clean_audit_fixture()
    )
    invalid_candidate = candidate.model_copy(
        update={"elaboration_status": ValidationStatus.INVALID}
    )

    audit = rule.audit(
        source,
        source_representation,
        invalid_candidate,
        candidate_representation,
        draft,
    )

    assert "candidate_does_not_elaborate" in audit.violation_codes
    assert audit.recommended_validation_status == ValidationStatus.QUARANTINED
    assert audit.recommended_quality_tier == QualityTier.UNKNOWN
    assert audit.metadata["semantic_negative_resolved"] is False


def test_audit_quarantines_unexpected_atom_or_tree_delta() -> None:
    rule, source, source_representation, candidate, candidate_representation, draft = (
        _clean_audit_fixture()
    )
    bad_atoms = candidate_representation.model_copy(
        update={
            "semantic_atoms": (*_atoms("1"), "const:Unexpected"),
            "content_hash": "a" * 64,
        }
    )
    bad_tree = candidate_representation.model_copy(
        update={
            "operator_tree": source_representation.operator_tree,
            "content_hash": "b" * 64,
        }
    )

    atom_audit = rule.audit(
        source,
        source_representation,
        candidate,
        bad_atoms,
        draft,
    )
    tree_audit = rule.audit(
        source,
        source_representation,
        candidate,
        bad_tree,
        draft,
    )

    assert "unexpected_semantic_atom_delta" in atom_audit.violation_codes
    assert atom_audit.atom_mapping_ok is False
    assert atom_audit.recommended_quality_tier == QualityTier.UNKNOWN
    assert "operator_tree_not_changed" in tree_audit.violation_codes
    assert tree_audit.structural_diff_ok is False
    assert tree_audit.recommended_quality_tier == QualityTier.UNKNOWN


@pytest.mark.parametrize(
    "failure",
    [
        "candidate_text_mismatch",
        "context_mismatch",
        "trace_tampered",
        "diff_tampered",
        "atom_mapping_tampered",
        "candidate_ancestry_tampered",
        "representation_lineage_tampered",
        "view_status_tampered",
        "statement_hash_tampered",
    ],
)
def test_audit_quarantines_lineage_trace_and_certificate_tampering(
    failure: str,
) -> None:
    rule, source, source_representation, candidate, candidate_representation, draft = (
        _clean_audit_fixture()
    )
    if failure == "candidate_text_mismatch":
        candidate_representation = candidate_representation.model_copy(
            update={
                "raw_proof_stripped": draft.candidate_code + " ",
                "content_hash": "c" * 64,
            }
        )
    elif failure == "context_mismatch":
        candidate_representation = candidate_representation.model_copy(
            update={"context_id": f"ctx:{'d' * 64}", "content_hash": "d" * 64}
        )
    elif failure == "trace_tampered":
        step = dict(draft.transformation_trace[0])
        step["literal_hash"] = "0" * 64
        draft = draft.model_copy(update={"transformation_trace": (step,)})
    elif failure == "diff_tampered":
        diff = dict(draft.expected_structural_diff)
        diff["source_span_start"] = 999
        draft = draft.model_copy(update={"expected_structural_diff": diff})
    elif failure == "atom_mapping_tampered":
        draft = draft.model_copy(update={"expected_atom_mapping": {"lit:nat:0": "lit:nat:0"}})
    elif failure == "candidate_ancestry_tampered":
        candidate = candidate.model_copy(update={"parent_theorem_ids": ()})
    elif failure == "representation_lineage_tampered":
        candidate_representation = candidate_representation.model_copy(
            update={
                "theorem_id": make_id("thm", {"n07": "wrong_candidate"}),
                "content_hash": "e" * 64,
            }
        )
    elif failure == "view_status_tampered":
        statuses = dict(candidate_representation.view_status)
        del statuses["semantic_atoms"]
        candidate_representation = candidate_representation.model_copy(
            update={"view_status": statuses, "content_hash": "f" * 64}
        )
    elif failure == "statement_hash_tampered":
        candidate = candidate.model_copy(update={"statement_content_hash": "0" * 64})

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
    assert audit.metadata["semantic_negative_resolved"] is False


def test_audit_rejects_trace_not_selected_by_recorded_seed() -> None:
    code = "theorem n07_fixture (n : Nat) : (0 < n) ∧ (n ≤ 1) := by sorry"
    source = _theorem(code)
    source_atoms = ("const:LT.lt", "lit:nat:0", "const:LE.le", "lit:nat:1")
    source_representation = _representation(
        code,
        semantic_atoms=source_atoms,
        tree={"root": {"k": "source"}, "node_count": 1, "depth": 1},
    )
    rule = _rule()
    by_site: dict[int, tuple[int, Any]] = {}
    for seed in range(128):
        draft = rule.generate(source, source_representation, seed)[0]
        start = draft.transformation_trace[0]["start"]
        assert isinstance(start, int)
        by_site.setdefault(start, (seed, draft))
        if len(by_site) == 2:
            break
    assert len(by_site) == 2
    (original_seed, draft), (other_seed, _other_draft) = by_site.values()
    assert original_seed != other_seed
    source_atom, target_atom = next(iter(draft.expected_atom_mapping.items()))
    candidate_atoms = Counter(source_atoms)
    candidate_atoms[source_atom] -= 1
    if candidate_atoms[source_atom] == 0:
        del candidate_atoms[source_atom]
    candidate_atoms[target_atom] += 1
    candidate = _candidate(source, draft.candidate_code)
    candidate_representation = _representation(
        draft.candidate_code,
        literal=target_atom.rsplit(":", 1)[1],
        theorem_id=candidate.theorem_id,
        representation_id=make_id("repr", {"n07_seed_tamper": draft.draft_id}),
        alpha=_CANDIDATE_ALPHA,
        semantic_atoms=tuple(candidate_atoms.elements()),
        tree={"root": {"k": "candidate"}, "node_count": 1, "depth": 1},
    )
    tampered = draft.model_copy(update={"seed": other_seed})

    audit = rule.audit(
        source,
        source_representation,
        candidate,
        candidate_representation,
        tampered,
    )

    assert "draft_id_mismatch" in audit.violation_codes
    assert "seed_site_selection_mismatch" in audit.violation_codes
    assert audit.recommended_quality_tier == QualityTier.UNKNOWN
    assert audit.structural_diff_ok is False
    assert audit.metadata["semantic_negative_resolved"] is False


def test_exact_atom_delta_changes_only_one_literal_occurrence() -> None:
    source = Counter(("lit:nat:0", "lit:nat:0", "const:LE.le"))
    candidate = Counter(("lit:nat:0", "lit:nat:1", "const:LE.le"))

    assert source - candidate == Counter({"lit:nat:0": 1})
    assert candidate - source == Counter({"lit:nat:1": 1})

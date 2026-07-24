"""LF-017 P02 conservative typed-binder regrouping."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from leanfaith.config.hashing import hash_canonical
from leanfaith.representations import alpha_identity_fingerprint
from leanfaith.representations.atoms import operator_tree, semantic_atoms
from leanfaith.schemas import (
    CANONICAL_VIEW_NAMES,
    QualityTier,
    ValidationStatus,
    ViewStatus,
    make_id,
)
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms import TransformationRegistry, load_transformation_registry
from leanfaith.transforms.positives.p02_binders import (
    BinderKind,
    P02BinderError,
    P02BinderRule,
    apply_exact_span_trace,
    binder_dependency_graph,
    enumerate_binder_edits,
    load_p02_binders_config,
    parse_typed_binders,
)
from tests.unit.record_factories import (
    REPR_A,
    THM_A,
    representation_record,
    theorem_record,
)

_REGISTRY_HASH = "a" * 64


def _expr_tree(*, conclusion: str = "True") -> dict[str, Any]:
    """∀ (α : Type) (x y : α), conclusion."""

    return {
        "k": "forall",
        "bi": "default",
        "dom": {"k": "sort", "u": "1"},
        "body": {
            "k": "forall",
            "bi": "default",
            "dom": {"k": "bvar", "i": 0},
            "body": {
                "k": "forall",
                "bi": "default",
                "dom": {"k": "bvar", "i": 1},
                "body": {"k": "const", "n": conclusion, "us": "[]"},
            },
        },
    }


def _theorem(code: str, **overrides: Any) -> TheoremRecord:
    payload: dict[str, Any] = {
        "proof_stripped_declaration": code,
        "statement_content_hash": hashlib.sha256(code.encode("utf-8")).hexdigest(),
        "declaration_name": "p02_fixture",
        "declaration_full_name": "p02_fixture",
    }
    payload.update(overrides)
    return theorem_record(**payload)


def _representation(
    code: str,
    *,
    tree: dict[str, Any] | None = None,
    theorem_id: str = THM_A,
    representation_id: str = REPR_A,
) -> RepresentationRecord:
    expression = tree or _expr_tree()
    op_tree = operator_tree(expression)
    atoms = semantic_atoms(expression)
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
    return representation_record(
        theorem_id=theorem_id,
        representation_id=representation_id,
        raw_proof_stripped=code,
        headless=code,
        signature_pp="fixture",
        signature_explicit="fixture",
        semantic_atoms=atoms,
        operator_tree=op_tree,
        alpha_identity_fingerprint=alpha_identity_fingerprint(expression),
        view_status=statuses,
        content_hash=hash_canonical(
            {
                "code": code,
                "tree": expression,
                "theorem_id": theorem_id,
            }
        ),
    )


def _candidate(
    source: TheoremRecord,
    code: str,
) -> TheoremRecord:
    theorem_id = make_id("thm", {"p02_candidate": code})
    return _theorem(
        code,
        theorem_id=theorem_id,
        ancestry_id=make_id("anc", {"p02_candidate": code}),
        root_ancestry_ids=source.root_ancestry_ids,
        parent_theorem_ids=(source.theorem_id,),
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
    )


def test_versioned_repository_config_is_strict_and_bound() -> None:
    loaded = load_p02_binders_config()

    assert loaded.config.rule_version == "1.0.0"
    assert loaded.config.supported_binder_kinds == (
        "explicit",
        "implicit",
        "strict_implicit",
    )
    assert loaded.config.currying_enabled is False
    assert loaded.config.instance_regroup_enabled is False
    assert len(loaded.config_hash) == 64


def test_config_loader_rejects_repository_escape(tmp_path: Path) -> None:
    outside = tmp_path / "p02.yaml"
    outside.write_text("schema_version: 1\n", encoding="utf-8")

    with pytest.raises(P02BinderError, match="escapes"):
        load_p02_binders_config(path=outside)


@pytest.mark.parametrize(
    ("source", "operation", "expected"),
    [
        (
            "theorem p02_fixture (x y : Nat) : x = x := by sorry",
            "split_group",
            "(x : Nat) (y : Nat)",
        ),
        (
            "theorem p02_fixture (x : Nat) (y : Nat) : x = x := by sorry",
            "merge_singletons",
            "(x y : Nat)",
        ),
        (
            "theorem p02_fixture {α β : Type} : True := by sorry",
            "split_group",
            "{α : Type} {β : Type}",
        ),
        (
            "theorem p02_fixture ⦃α β : Type⦄ : True := by sorry",
            "split_group",
            "⦃α : Type⦄ ⦃β : Type⦄",
        ),
    ],
)
def test_grouped_and_ungrouped_binder_edits(
    source: str,
    operation: str,
    expected: str,
) -> None:
    edits = enumerate_binder_edits(source)

    assert len(edits) == 1
    assert edits[0].operation == operation
    assert edits[0].binder_kind in {
        BinderKind.EXPLICIT,
        BinderKind.IMPLICIT,
        BinderKind.STRICT_IMPLICIT,
    }
    transformed = source[: edits[0].start] + edits[0].replacement_text + source[edits[0].end :]
    assert expected in transformed


def test_safe_dependency_on_an_earlier_binder_is_preserved() -> None:
    source = "theorem p02_fixture (α : Type) (x y : α) : x = x := by sorry"

    edits = enumerate_binder_edits(source)

    assert [edit.names for edit in edits] == [("x", "y")]


def test_dependent_singletons_with_same_outer_dependency_can_merge() -> None:
    source = "theorem p02_fixture (n : Nat) (x : Fin n) (y : Fin n) : x = x := by sorry"

    edits = enumerate_binder_edits(source)

    assert len(edits) == 1
    assert edits[0].names == ("x", "y")
    assert edits[0].replacement_text == "(x y : Fin n)"


@pytest.mark.parametrize(
    "source",
    [
        # Splitting would make the second type resolve the newly introduced
        # shadowing x rather than the outer x.
        "theorem p02_fixture (x : Nat) (x y : Fin x) : True := by sorry",
        # A direct dependency means these differently typed binders are not a
        # regrouping candidate.
        "theorem p02_fixture (n : Nat) (x : Fin n) : True := by sorry",
        # Instance binders have a distinct Lean grammar: [i j : C] is invalid.
        "theorem p02_fixture {α : Type} [i : Inhabited α] [j : Inhabited α] : True := by sorry",
    ],
)
def test_unsafe_dependent_shadowing_and_instance_cases_are_rejected(source: str) -> None:
    rule = P02BinderRule(registry_hash=_REGISTRY_HASH)
    theorem = _theorem(source)
    representation = _representation(source)

    applicability = rule.assess(theorem, representation)

    assert not applicability.applicable
    assert rule.generate(theorem, representation, 3) == ()


def test_comments_strings_and_unicode_are_lexically_safe() -> None:
    source = (
        "/-- parentheses (inside docs) and a nested /- comment -/ -/\n"
        "theorem «p02 unicode» (α β : Type) (x y : α) : "
        '(("(" : String).length = 1) := by sorry'
    )

    binders = parse_typed_binders(source)
    edits = enumerate_binder_edits(source)

    assert [binder.names for binder in binders] == [("α", "β"), ("x", "y")]
    for edit in edits:
        transformed = source[: edit.start] + edit.replacement_text + source[edit.end :]
        assert source.splitlines()[0] in transformed
        assert '"("' in transformed
        assert "«p02 unicode»" in transformed


@pytest.mark.parametrize(
    "source",
    [
        "theorem p02_fixture (x /- retained -/ y : Nat) : True := by sorry",
        "theorem p02_fixture (x : Nat) /- boundary -/ (y : Nat) : True := by sorry",
    ],
)
def test_comments_in_or_between_candidate_binders_disable_the_edit(source: str) -> None:
    assert enumerate_binder_edits(source) == ()


@pytest.mark.parametrize("seed", range(32))
def test_seeded_generation_is_deterministic_and_exactly_invertible(seed: int) -> None:
    code = "theorem p02_fixture (α β : Type) (x y : α) {p q : Prop} : x = x := by sorry"
    theorem = _theorem(code)
    representation = _representation(code)
    rule = P02BinderRule(registry_hash=_REGISTRY_HASH)

    first = rule.generate(theorem, representation, seed)
    replay = rule.generate(theorem, representation, seed)

    assert first == replay
    assert len(first) == 1
    draft = first[0]
    assert draft.intended_relation.value == "equivalent"
    assert draft.candidate_pool == "deterministic_positive_provisional"
    assert draft.inverse_trace is not None
    assert apply_exact_span_trace(draft.candidate_code, draft.inverse_trace) == code
    assert draft.expected_structural_diff["currying_applied"] is False
    assert draft.transformation_trace[0]["rule_config_hash"] == rule.rule_config_hash
    assert draft.expected_structural_diff["rule_config_hash"] == rule.rule_config_hash


def test_dependency_graph_records_outer_binder_edges() -> None:
    graph = binder_dependency_graph(operator_tree(_expr_tree()))

    assert [node.depends_on for node in graph] == [(), (0,), (0,)]
    assert [node.binder_info for node in graph] == ["default", "default", "default"]


def test_mechanical_audit_accepts_only_as_provisional() -> None:
    code = "theorem p02_fixture (α : Type) (x y : α) : True := by sorry"
    source = _theorem(code)
    source_representation = _representation(code)
    rule = P02BinderRule(registry_hash=_REGISTRY_HASH)
    draft = rule.generate(source, source_representation, 7)[0]
    candidate = _candidate(source, draft.candidate_code)
    candidate_representation = _representation(
        draft.candidate_code,
        theorem_id=candidate.theorem_id,
        representation_id=make_id("repr", {"p02_candidate": draft.draft_id}),
    )

    audit = rule.audit(
        source,
        source_representation,
        candidate,
        candidate_representation,
        draft,
    )

    assert audit.violation_codes == ()
    assert audit.structural_diff_ok
    assert audit.atom_mapping_ok
    assert audit.inverse_or_roundtrip_ok
    assert audit.recommended_quality_tier == QualityTier.PROVISIONAL
    assert audit.metadata["alpha_identity_equal"] is True
    assert audit.metadata["binder_dependency_graph_equal"] is True
    assert audit.metadata["currying_applied"] is False


def test_audit_quarantines_alpha_or_atom_drift() -> None:
    code = "theorem p02_fixture (α : Type) (x y : α) : True := by sorry"
    source = _theorem(code)
    source_representation = _representation(code)
    rule = P02BinderRule(registry_hash=_REGISTRY_HASH)
    draft = rule.generate(source, source_representation, 7)[0]
    candidate = _candidate(source, draft.candidate_code)
    candidate_representation = _representation(
        draft.candidate_code,
        tree=_expr_tree(conclusion="False"),
        theorem_id=candidate.theorem_id,
        representation_id=make_id("repr", {"p02_bad": draft.draft_id}),
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
    assert "semantic_atoms_mismatch" in audit.violation_codes


def test_assess_rejects_non_elaborating_or_text_mismatched_source() -> None:
    code = "theorem p02_fixture (x y : Nat) : True := by sorry"
    rule = P02BinderRule(registry_hash=_REGISTRY_HASH)

    invalid = rule.assess(
        _theorem(code, elaboration_status=ValidationStatus.INVALID),
        _representation(code),
    )
    mismatched = rule.assess(_theorem(code), _representation(code + " "))

    assert invalid.reason_codes == ("source_does_not_elaborate",)
    assert mismatched.reason_codes == ("source_representation_text_mismatch",)


def test_audit_quarantines_mismatched_candidate_representation_text() -> None:
    code = "theorem p02_fixture (x y : Nat) : True := by sorry"
    source = _theorem(code)
    source_representation = _representation(code)
    rule = P02BinderRule(registry_hash=_REGISTRY_HASH)
    draft = rule.generate(source, source_representation, 7)[0]
    candidate = _candidate(source, draft.candidate_code)
    candidate_representation = _representation(
        draft.candidate_code + " ",
        theorem_id=candidate.theorem_id,
        representation_id=make_id("repr", {"p02_text_mismatch": draft.draft_id}),
    )

    audit = rule.audit(
        source,
        source_representation,
        candidate,
        candidate_representation,
        draft,
    )

    assert audit.recommended_validation_status == ValidationStatus.QUARANTINED
    assert audit.recommended_quality_tier == QualityTier.UNKNOWN
    assert "candidate_representation_text_mismatch" in audit.violation_codes


@pytest.mark.parametrize("tamper", ["trace", "expected_diff", "atom_mapping"])
def test_audit_quarantines_tampered_certificate_fields(tamper: str) -> None:
    code = "theorem p02_fixture (x y : Nat) : True := by sorry"
    source = _theorem(code)
    source_representation = _representation(code)
    rule = P02BinderRule(registry_hash=_REGISTRY_HASH)
    draft = rule.generate(source, source_representation, 7)[0]
    candidate = _candidate(source, draft.candidate_code)
    candidate_representation = _representation(
        draft.candidate_code,
        theorem_id=candidate.theorem_id,
        representation_id=make_id("repr", {"p02_tamper": tamper}),
    )
    if tamper == "trace":
        step = dict(draft.transformation_trace[0])
        step["type_token_hash"] = "0" * 64
        draft = draft.model_copy(update={"transformation_trace": (step,)})
    elif tamper == "expected_diff":
        expected = dict(draft.expected_structural_diff)
        expected["source_name_count"] = 99
        draft = draft.model_copy(update={"expected_structural_diff": expected})
    else:
        draft = draft.model_copy(update={"expected_atom_mapping": {"source:Nat": "candidate:Int"}})

    audit = rule.audit(
        source,
        source_representation,
        candidate,
        candidate_representation,
        draft,
    )

    assert audit.recommended_validation_status == ValidationStatus.QUARANTINED
    assert audit.recommended_quality_tier == QualityTier.UNKNOWN
    expected_violation = (
        "semantic_atoms_mismatch" if tamper == "atom_mapping" else "structural_diff_mismatch"
    )
    assert expected_violation in audit.violation_codes


def test_audit_quarantines_representation_theorem_lineage_mismatch() -> None:
    code = "theorem p02_fixture (x y : Nat) : True := by sorry"
    source = _theorem(code)
    source_representation = _representation(code)
    rule = P02BinderRule(registry_hash=_REGISTRY_HASH)
    draft = rule.generate(source, source_representation, 7)[0]
    candidate = _candidate(source, draft.candidate_code)
    candidate_representation = _representation(
        draft.candidate_code,
        theorem_id=THM_A,
        representation_id=make_id("repr", {"p02_wrong_theorem": draft.draft_id}),
    )

    audit = rule.audit(
        source,
        source_representation,
        candidate,
        candidate_representation,
        draft,
    )

    assert audit.recommended_validation_status == ValidationStatus.QUARANTINED
    assert audit.recommended_quality_tier == QualityTier.UNKNOWN
    assert "representation_lineage_mismatch" in audit.violation_codes


def test_exact_trace_fails_closed_on_source_drift() -> None:
    trace: Sequence[dict[str, object]] = (
        {
            "operation": "replace_exact_span",
            "start": 0,
            "end": 1,
            "expected_text": "x",
            "replacement_text": "y",
        },
    )

    with pytest.raises(ValueError, match="expected_text_mismatch"):
        apply_exact_span_trace("z", trace)


def test_repository_registry_dispatches_available_p02() -> None:
    loaded = load_transformation_registry()
    runtime = TransformationRegistry(loaded)
    runtime.register(P02BinderRule(registry_hash=loaded.registry_hash))
    code = "theorem p02_fixture (x y : Nat) : True := by sorry"

    execution = runtime.execute(
        "p02_binders",
        _theorem(code),
        _representation(code),
        11,
    )

    assert execution.attempt.terminal_outcome == "generated"
    assert len(execution.drafts) == 1
    assert execution.drafts[0].family_id == "p02_binders"

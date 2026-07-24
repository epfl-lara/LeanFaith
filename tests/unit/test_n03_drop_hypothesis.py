"""LF-018 N03 independent proposition-hypothesis deletion tests."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from leanfaith.config.hashing import hash_canonical
from leanfaith.representations import alpha_identity_fingerprint
from leanfaith.representations.atoms import operator_tree, semantic_atoms
from leanfaith.schemas import (
    CANONICAL_VIEW_NAMES,
    IntendedRelation,
    QualityTier,
    ValidationStatus,
    ViewStatus,
    make_id,
)
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms.negatives.n03_drop_hypothesis import (
    N03DropHypothesisError,
    N03DropHypothesisRule,
    analyze_outer_foralls,
    apply_hypothesis_trace,
    enumerate_independent_prop_hypotheses,
    erase_outer_forall,
    load_n03_drop_hypothesis_config,
)
from tests.unit.record_factories import REPR_A, THM_A, representation_record, theorem_record

_REGISTRY_HASH = "7" * 64


def _prop_hypothesis_tree(
    *,
    conclusion: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Elaborated type of ``(P : Prop) (h : P) : conclusion``."""

    return {
        "k": "forall",
        "bi": "default",
        "dom": {"k": "sort", "u": "0"},
        "body": {
            "k": "forall",
            "bi": "default",
            "dom": {"k": "bvar", "i": 0},
            "body": conclusion or {"k": "const", "n": "True", "us": "[]"},
        },
    }


def _two_hypothesis_tree() -> dict[str, Any]:
    """``(P Q : Prop) (h : P) (q : Q) : True``."""

    return {
        "k": "forall",
        "bi": "default",
        "dom": {"k": "sort", "u": "0"},
        "body": {
            "k": "forall",
            "bi": "default",
            "dom": {"k": "sort", "u": "0"},
            "body": {
                "k": "forall",
                "bi": "default",
                "dom": {"k": "bvar", "i": 1},
                "body": {
                    "k": "forall",
                    "bi": "default",
                    "dom": {"k": "bvar", "i": 1},
                    "body": {"k": "const", "n": "True", "us": "[]"},
                },
            },
        },
    }


def _later_dependent_tree() -> dict[str, Any]:
    """``(P : Prop) (h : P) (q : h = h) : True``."""

    equality = {
        "k": "app",
        "fn": {
            "k": "app",
            "fn": {
                "k": "app",
                "fn": {"k": "const", "n": "Eq", "us": "[1]"},
                "arg": {"k": "bvar", "i": 1},
            },
            "arg": {"k": "bvar", "i": 0},
        },
        "arg": {"k": "bvar", "i": 0},
    }
    return {
        "k": "forall",
        "bi": "default",
        "dom": {"k": "sort", "u": "0"},
        "body": {
            "k": "forall",
            "bi": "default",
            "dom": {"k": "bvar", "i": 0},
            "body": {
                "k": "forall",
                "bi": "default",
                "dom": equality,
                "body": {"k": "const", "n": "True", "us": "[]"},
            },
        },
    }


def _conclusion_dependent_tree() -> dict[str, Any]:
    """``(P : Prop) (h : P) : h = h``."""

    equality = {
        "k": "app",
        "fn": {
            "k": "app",
            "fn": {
                "k": "app",
                "fn": {"k": "const", "n": "Eq", "us": "[1]"},
                "arg": {"k": "bvar", "i": 1},
            },
            "arg": {"k": "bvar", "i": 0},
        },
        "arg": {"k": "bvar", "i": 0},
    }
    return _prop_hypothesis_tree(conclusion=equality)


def _theorem(code: str, **overrides: Any) -> TheoremRecord:
    payload: dict[str, Any] = {
        "proof_stripped_declaration": code,
        "statement_content_hash": hashlib.sha256(code.encode("utf-8")).hexdigest(),
        "declaration_name": "n03_fixture",
        "declaration_full_name": "n03_fixture",
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
    tree: dict[str, Any],
    *,
    theorem_id: str = THM_A,
    representation_id: str = REPR_A,
    signature: str = "source signature",
    context_id: str | None = None,
    valid_views: bool = True,
) -> RepresentationRecord:
    op_tree = operator_tree(tree) if valid_views else None
    atoms = semantic_atoms(tree) if valid_views else None
    alpha = alpha_identity_fingerprint(tree) if valid_views else None
    statuses = dict.fromkeys(CANONICAL_VIEW_NAMES, ViewStatus.NOT_ATTEMPTED)
    for view in ("raw_proof_stripped", "headless"):
        statuses[view] = ViewStatus.OK
    for view in ("signature_pp", "signature_explicit", "semantic_atoms", "operator_tree"):
        statuses[view] = ViewStatus.OK if valid_views else ViewStatus.FAILED
    payload: dict[str, Any] = {
        "theorem_id": theorem_id,
        "representation_id": representation_id,
        "raw_proof_stripped": code,
        "headless": code,
        "signature_pp": signature if valid_views else None,
        "signature_explicit": signature if valid_views else None,
        "semantic_atoms": atoms,
        "operator_tree": op_tree,
        "alpha_identity_fingerprint": alpha,
        "view_status": statuses,
        "content_hash": hash_canonical(
            {
                "alpha": alpha,
                "code": code,
                "signature": signature,
                "theorem_id": theorem_id,
                "tree": tree,
                "valid_views": valid_views,
            }
        ),
    }
    if context_id is not None:
        payload["context_id"] = context_id
    return representation_record(**payload)


def _rule() -> N03DropHypothesisRule:
    return N03DropHypothesisRule.from_repository(registry_hash=_REGISTRY_HASH)


def _candidate(source: TheoremRecord, code: str, *, valid: bool = True) -> TheoremRecord:
    return _theorem(
        code,
        theorem_id=make_id("thm", {"n03_candidate": code}),
        ancestry_id=make_id("anc", {"n03_candidate": code}),
        root_ancestry_ids=source.root_ancestry_ids,
        parent_theorem_ids=(source.theorem_id,),
        elaboration_status=(
            ValidationStatus.ELABORATES_WITH_PLACEHOLDER if valid else ValidationStatus.INVALID
        ),
    )


def test_config_is_strict_and_explicitly_ignores_failed_proof_search() -> None:
    loaded = load_n03_drop_hypothesis_config()

    assert loaded.config.rule_version == "1.0.0"
    assert loaded.config.proposition_domain_policy == "direct_declared_prop_variable"
    assert loaded.config.intended_error_types == ("E01",)
    assert loaded.config.failed_proof_search_is_negative_evidence is False
    assert len(loaded.config_hash) == 64


def test_dependency_analysis_proves_independence_and_exact_expr_erasure() -> None:
    tree = _prop_hypothesis_tree()
    analysis = analyze_outer_foralls(operator_tree(tree))

    assert [binder.depends_on for binder in analysis.binders] == [(), (0,)]
    assert analysis.conclusion_depends_on == ()
    assert erase_outer_forall(tree, 1) == {
        "k": "forall",
        "bi": "default",
        "dom": {"k": "sort", "u": "0"},
        "body": {"k": "const", "n": "True", "us": "[]"},
    }


def test_enumerator_accepts_only_exact_independent_singleton_prop_hypothesis() -> None:
    code = "theorem n03_fixture (P : Prop) (h : P) : True := by sorry"
    sites = enumerate_independent_prop_hypotheses(code, operator_tree(_prop_hypothesis_tree()))

    assert len(sites) == 1
    assert sites[0].hypothesis_name == "h"
    assert sites[0].proposition_name == "P"
    assert code[sites[0].start : sites[0].end] == "(h : P)"


def test_declaration_scanner_ignores_comments_strings_and_guillemet_names() -> None:
    code = (
        "/- theorem fake : False := by sorry -/\n"
        "theorem «lemma theorem» (P : Prop) (h : P) : "
        '("theorem" = "theorem") := by sorry'
    )
    sites = enumerate_independent_prop_hypotheses(
        code,
        operator_tree(
            _prop_hypothesis_tree(
                conclusion={
                    "k": "app",
                    "fn": {
                        "k": "app",
                        "fn": {
                            "k": "app",
                            "fn": {"k": "const", "n": "Eq", "us": "[1]"},
                            "arg": {"k": "const", "n": "String", "us": "[]"},
                        },
                        "arg": {"k": "lit", "str": "theorem"},
                    },
                    "arg": {"k": "lit", "str": "theorem"},
                }
            )
        ),
    )

    assert len(sites) == 1
    assert sites[0].hypothesis_name == "h"


@pytest.mark.parametrize(
    "code",
    [
        (
            "theorem n03_fixture (P : Prop) (h : P) : True := by sorry\n"
            "theorem second : True := by sorry"
        ),
        "theorem n03_fixture (P : Prop) (h : P) : True := by exact True.intro",
    ],
)
def test_multiple_declarations_and_real_proof_bodies_are_rejected(code: str) -> None:
    applicability = _rule().assess(
        _theorem(code),
        _representation(code, _prop_hypothesis_tree()),
    )

    assert not applicability.applicable


@pytest.mark.parametrize(
    ("code", "tree"),
    [
        (
            "theorem n03_fixture (n : Nat) : True := by sorry",
            {
                "k": "forall",
                "bi": "default",
                "dom": {"k": "const", "n": "Nat", "us": "[]"},
                "body": {"k": "const", "n": "True", "us": "[]"},
            },
        ),
        (
            "theorem n03_fixture (P : Prop) {h : P} : True := by sorry",
            {
                **_prop_hypothesis_tree(),
                "body": {
                    **_prop_hypothesis_tree()["body"],
                    "bi": "implicit",
                },
            },
        ),
        (
            "theorem n03_fixture (P : Prop) (h k : P) : True := by sorry",
            {
                "k": "forall",
                "bi": "default",
                "dom": {"k": "sort", "u": "0"},
                "body": {
                    "k": "forall",
                    "bi": "default",
                    "dom": {"k": "bvar", "i": 0},
                    "body": {
                        "k": "forall",
                        "bi": "default",
                        "dom": {"k": "bvar", "i": 1},
                        "body": {"k": "const", "n": "True", "us": "[]"},
                    },
                },
            },
        ),
        (
            "theorem n03_fixture (P : Prop) (h : P) (q : h = h) : True := by sorry",
            _later_dependent_tree(),
        ),
        (
            "theorem n03_fixture (P : Prop) (h : P) : h = h := by sorry",
            _conclusion_dependent_tree(),
        ),
    ],
)
def test_unsupported_hypothesis_shapes_or_dependencies_are_rejected(
    code: str,
    tree: dict[str, Any],
) -> None:
    assert enumerate_independent_prop_hypotheses(code, operator_tree(tree)) == ()


def test_auto_implicit_header_is_rejected_before_site_enumeration() -> None:
    code = "theorem n03_fixture (h : P) : True := by sorry"
    tree = {
        "k": "forall",
        "bi": "implicit",
        "dom": {"k": "sort", "u": "0"},
        "body": _prop_hypothesis_tree()["body"],
    }
    applicability = _rule().assess(_theorem(code), _representation(code, tree))

    assert not applicability.applicable
    assert applicability.reason_codes == ("surface_elaboration_binder_alignment_mismatch",)


def test_source_preconditions_fail_closed() -> None:
    code = "theorem n03_fixture (P : Prop) (h : P) : True := by sorry"
    tree = _prop_hypothesis_tree()
    invalid = _theorem(code, elaboration_status=ValidationStatus.INVALID)
    mismatched = _representation(code + " ", tree)
    missing = _representation(code, tree, valid_views=False)
    wrong_context = _representation(code, tree, context_id="ctx:" + "9" * 64)

    assert _rule().assess(invalid, _representation(code, tree)).reason_codes == (
        "source_does_not_elaborate",
    )
    assert _rule().assess(_theorem(code), mismatched).reason_codes == (
        "source_representation_text_mismatch",
    )
    assert _rule().assess(_theorem(code), missing).reason_codes == ("source_required_view_missing",)
    assert _rule().assess(_theorem(code), wrong_context).reason_codes == (
        "source_context_mismatch",
    )


def test_source_dependency_views_and_expr_scope_are_revalidated_before_generation() -> None:
    code = "theorem n03_fixture (P : Prop) (h : P) : True := by sorry"
    tree = _prop_hypothesis_tree()
    theorem = _theorem(code)
    atoms_tampered = _representation(code, tree).model_copy(
        update={"semantic_atoms": ("const:False",)}
    )
    proof_field_tree = {
        **tree,
        "proof": {"k": "const", "n": "forged.proof", "us": "[]"},
    }
    proof_field_representation = _representation(code, proof_field_tree)
    out_of_scope_tree = {
        **tree,
        "body": {
            **tree["body"],
            "dom": {"k": "bvar", "i": 1},
        },
    }
    out_of_scope_representation = _representation(code, out_of_scope_tree)

    assert _rule().assess(theorem, atoms_tampered).reason_codes == (
        "source_derived_views_inconsistent",
    )
    assert _rule().assess(theorem, proof_field_representation).reason_codes == (
        "malformed_operator_tree:malformed_expr_node_shape:forall",
    )
    assert _rule().assess(theorem, out_of_scope_representation).reason_codes == (
        "malformed_operator_tree:out_of_scope_bvar",
    )


def test_generation_is_seeded_exact_and_invertible() -> None:
    code = "theorem n03_fixture (P Q : Prop) (h : P) (q : Q) : True := by sorry"
    source = _theorem(code)
    representation = _representation(code, _two_hypothesis_tree())
    rule = _rule()
    selected: set[str] = set()

    for seed in range(64):
        first = rule.generate(source, representation, seed)[0]
        replay = rule.generate(source, representation, seed)[0]
        assert first == replay
        assert first.intended_relation == IntendedRelation.NEAR_MISS
        assert first.intended_error_types == ("E01",)
        assert first.inverse_trace is not None
        assert (
            apply_hypothesis_trace(
                first.candidate_code,
                first.inverse_trace,
                expected_rule_config_hash=rule.rule_config_hash,
            )
            == code
        )
        selected.add(str(first.transformation_trace[0]["hypothesis_name"]))

    assert selected == {"h", "q"}


def test_trace_rejects_source_config_and_span_drift() -> None:
    code = "theorem n03_fixture (P : Prop) (h : P) : True := by sorry"
    rule = _rule()
    draft = rule.generate(
        _theorem(code),
        _representation(code, _prop_hypothesis_tree()),
        3,
    )[0]

    with pytest.raises(N03DropHypothesisError, match="input_code_hash"):
        apply_hypothesis_trace(code.replace("(P : Prop)", "(P:Prop)"), draft.transformation_trace)
    with pytest.raises(N03DropHypothesisError, match="rule_config_hash"):
        apply_hypothesis_trace(
            code,
            draft.transformation_trace,
            expected_rule_config_hash="0" * 64,
        )
    tampered = (dict(draft.transformation_trace[0], start=0),)
    with pytest.raises(N03DropHypothesisError, match="expected_text"):
        apply_hypothesis_trace(code, tampered)


def test_clean_audit_accepts_extraction_style_source_hash_and_remains_provisional() -> None:
    code = "theorem n03_fixture (P : Prop) (h : P) : True := by sorry"
    source_tree = _prop_hypothesis_tree()
    source = _theorem(
        code,
        statement_content_hash=_extraction_style_statement_hash(code),
    )
    source_representation = _representation(code, source_tree)
    rule = _rule()
    draft = rule.generate(source, source_representation, 7)[0]
    candidate = _candidate(source, draft.candidate_code)
    candidate_tree = erase_outer_forall(source_tree, 1)
    candidate_representation = _representation(
        draft.candidate_code,
        candidate_tree,
        theorem_id=candidate.theorem_id,
        representation_id=make_id("repr", {"n03_candidate": draft.draft_id}),
        signature="candidate signature",
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
    assert audit.metadata["failed_proof_search_consulted"] is False
    assert audit.metadata["semantic_negative_resolved"] is False


@pytest.mark.parametrize(
    "failure",
    [
        "candidate_invalid",
        "candidate_text_mismatch",
        "candidate_tree_mismatch",
        "candidate_wrapper_mismatch",
        "candidate_atoms_mismatch",
        "candidate_alpha_mismatch",
        "candidate_ancestry_mismatch",
        "candidate_statement_hash_mismatch",
        "source_tree_forged",
        "draft_candidate_hash_mismatch",
        "draft_identity_mismatch",
        "draft_metadata_claims_negative",
        "draft_metadata_injects_label",
        "trace_tampered",
        "diff_tampered",
        "context_mismatch",
    ],
)
def test_audit_quarantines_every_lineage_elaboration_or_view_violation(
    failure: str,
) -> None:
    code = "theorem n03_fixture (P : Prop) (h : P) : True := by sorry"
    source_tree = _prop_hypothesis_tree()
    source = _theorem(code)
    source_representation = _representation(code, source_tree)
    rule = _rule()
    draft = rule.generate(source, source_representation, 7)[0]
    candidate = _candidate(
        source,
        draft.candidate_code,
        valid=failure != "candidate_invalid",
    )
    candidate_tree = erase_outer_forall(source_tree, 1)
    if failure == "candidate_tree_mismatch":
        candidate_tree = {"k": "const", "n": "True", "us": "[]"}
    candidate_code = (
        draft.candidate_code + " " if failure == "candidate_text_mismatch" else draft.candidate_code
    )
    candidate_representation = _representation(
        candidate_code,
        candidate_tree,
        theorem_id=candidate.theorem_id,
        representation_id=make_id("repr", {"n03_failure": failure}),
        signature="candidate signature",
        context_id=("ctx:" + "9" * 64 if failure == "context_mismatch" else None),
    )
    if failure == "candidate_wrapper_mismatch":
        assert candidate_representation.operator_tree is not None
        forged_wrapper = dict(candidate_representation.operator_tree)
        node_count = forged_wrapper["node_count"]
        assert isinstance(node_count, int)
        forged_wrapper["node_count"] = node_count + 1
        candidate_representation = candidate_representation.model_copy(
            update={"operator_tree": forged_wrapper}
        )
    elif failure == "candidate_atoms_mismatch":
        candidate_representation = candidate_representation.model_copy(
            update={"semantic_atoms": ("const:False",)}
        )
    elif failure == "candidate_alpha_mismatch":
        candidate_representation = candidate_representation.model_copy(
            update={"alpha_identity_fingerprint": "9" * 64}
        )
    elif failure == "trace_tampered":
        step = dict(draft.transformation_trace[0])
        step["dependency_proof_hash"] = "0" * 64
        draft = draft.model_copy(update={"transformation_trace": (step,)})
    elif failure == "diff_tampered":
        diff = dict(draft.expected_structural_diff)
        diff["outer_binder_index"] = 999
        draft = draft.model_copy(update={"expected_structural_diff": diff})
    elif failure == "candidate_ancestry_mismatch":
        candidate = candidate.model_copy(update={"parent_theorem_ids": ()})
    elif failure == "candidate_statement_hash_mismatch":
        candidate = candidate.model_copy(update={"statement_content_hash": "0" * 64})
    elif failure == "source_tree_forged":
        assert source_representation.operator_tree is not None
        forged_source_tree = dict(source_representation.operator_tree)
        forged_source_tree["root"] = {
            **source_tree,
            "proof": {"k": "const", "n": "forged.proof", "us": "[]"},
        }
        source_representation = source_representation.model_copy(
            update={"operator_tree": forged_source_tree}
        )
    elif failure == "draft_candidate_hash_mismatch":
        draft = draft.model_copy(update={"candidate_code_hash": "0" * 64})
    elif failure == "draft_identity_mismatch":
        draft = draft.model_copy(update={"candidate_pool": "forged_pool"})
    elif failure == "draft_metadata_claims_negative":
        draft = draft.model_copy(
            update={"metadata": {**draft.metadata, "semantic_negative_resolved": True}}
        )
    elif failure == "draft_metadata_injects_label":
        draft = draft.model_copy(update={"metadata": {**draft.metadata, "same_claim": False}})

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

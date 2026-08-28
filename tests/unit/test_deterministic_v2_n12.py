"""Focused LF-034 tests for the provisional N12 D0 family."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast

import pytest

from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.lean.protocol import LeanStatus
from leanfaith.representations import TheoremForRepresentation, alpha_identity_fingerprint
from leanfaith.representations.atoms import operator_tree, semantic_atoms
from leanfaith.schemas import CANONICAL_VIEW_NAMES, ViewStatus, make_id
from leanfaith.schemas.enums import IntendedRelation, QualityTier, ValidationStatus
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms.materialize import build_derived_theorem_record
from leanfaith.transforms.negatives.n12_implication_converse import (
    N12ImplicationConverseError,
    N12ImplicationConverseRule,
    apply_n12_trace,
    build_implication_converse_root,
    certify_implication_converse,
    enumerate_n12_sites,
)
from leanfaith.transforms.v2_d0_n12_runtime import (
    V2D0N12ExecutionError,
    build_v2_d0_n12_runtime,
    load_v2_d0_n12_execution_config,
)
from leanfaith.transforms.v2_d0_scale_run import run_v2_d0_scale
from tests.unit.record_factories import representation_record, theorem_record
from tests.unit.test_deterministic_v2_n11_scale import _BatchBackend

_SOURCE = "theorem n12 (Premise Goal : Prop) (h : Premise) : Goal := by sorry"


def _root(*, converse: bool = False) -> dict[str, object]:
    prop = {"k": "sort", "u": "0"}
    return {
        "k": "forall",
        "bi": "default",
        "dom": prop,
        "body": {
            "k": "forall",
            "bi": "default",
            "dom": prop,
            "body": {
                "k": "forall",
                "bi": "default",
                "dom": {"k": "bvar", "i": 0 if converse else 1},
                "body": {"k": "bvar", "i": 2 if converse else 1},
            },
        },
    }


def _complex_root(*, converse: bool = False) -> dict[str, object]:
    true = {"k": "const", "n": "True", "us": "[]"}
    false = {"k": "const", "n": "False", "us": "[]"}
    premise = {
        "k": "app",
        "fn": {"k": "app", "fn": {"k": "const", "n": "And", "us": "[]"}, "arg": true},
        "arg": true,
    }
    conclusion = {
        "k": "app",
        "fn": {
            "k": "app",
            "fn": {"k": "const", "n": "Or", "us": "[]"},
            "arg": true,
        },
        "arg": false,
    }
    return {
        "k": "forall",
        "bi": "default",
        "dom": conclusion if converse else premise,
        "body": premise if converse else conclusion,
    }


def _nested_root() -> dict[str, object]:
    root = _root()
    assert isinstance(root["body"], dict)
    assert isinstance(root["body"]["body"], dict)
    root["body"]["body"]["body"] = {
        "k": "forall",
        "bi": "default",
        "dom": {"k": "bvar", "i": 1},
        "body": {"k": "bvar", "i": 3},
    }
    return root


def _records(
    source: str,
    key: str,
    root: dict[str, object],
) -> tuple[TheoremRecord, RepresentationRecord]:
    theorem_id = make_id("thm", {"n12": key})
    ancestry_id = make_id("anc", {"n12": key})
    theorem = theorem_record(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        declaration_name="n12",
        declaration_full_name="n12",
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
        representation_id=make_id("repr", {"n12": key}),
        theorem_id=theorem_id,
        raw_proof_stripped=source,
        headless="(Premise Goal : Prop) (h : Premise) : Goal",
        signature_pp="∀ (Premise Goal : Prop), Premise → Goal",
        signature_explicit="∀ (Premise Goal : Prop), Premise → Goal",
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
) -> tuple[TheoremRecord, RepresentationRecord]:
    rule = N12ImplicationConverseRule(
        generation_config_hash="b" * 64,
        candidate_pool="fixture",
    )
    draft = rule.generate(source, source_representation, seed=7)[0]
    assert draft.candidate_code == draft_code
    candidate = build_derived_theorem_record(
        draft=draft,
        sources=(source,),
        primary_source_id=source.theorem_id,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        inline_elaboration_source="import LeanFaithFixtures\n" + draft_code,
    )
    candidate_root = _root(converse=True)
    candidate_representation = source_representation.model_copy(
        update={
            "representation_id": make_id("repr", {"n12_candidate": candidate.theorem_id}),
            "theorem_id": candidate.theorem_id,
            "raw_proof_stripped": draft_code,
            "signature_explicit": "∀ (Premise Goal : Prop), Goal → Premise",
            "semantic_atoms": semantic_atoms(candidate_root),
            "operator_tree": operator_tree(candidate_root),
            "alpha_identity_fingerprint": alpha_identity_fingerprint(candidate_root),
        }
    )
    return candidate, candidate_representation


def test_n12_enumerates_one_surface_and_expr_aligned_site() -> None:
    (site,) = enumerate_n12_sites(_SOURCE, operator_tree(_root()))
    assert site.hypothesis_name == "h"
    assert site.premise_text == "Premise"
    assert site.conclusion_text == "Goal"
    assert site.hypothesis_outer_index == 2
    assert build_implication_converse_root(_root(), 2) == _root(converse=True)


def test_n12_supports_complex_root_proposition_sides() -> None:
    source = "theorem n12_complex (h : True ∧ True) : True ∨ False := by sorry"
    (site,) = enumerate_n12_sites(source, operator_tree(_complex_root()))
    assert site.premise_text == "True ∧ True"
    assert site.conclusion_text == "True ∨ False"
    assert build_implication_converse_root(_complex_root(), 0) == _complex_root(converse=True)

    theorem, representation = _records(source, "complex", _complex_root())
    rule = N12ImplicationConverseRule(
        generation_config_hash="b" * 64,
        candidate_pool="fixture",
    )
    draft = rule.generate(theorem, representation, seed=7)[0]
    assert draft.candidate_code == (
        "theorem n12_complex (h : True ∨ False) : True ∧ True := by sorry"
    )


@pytest.mark.parametrize(
    ("source", "root"),
    [
        (
            "theorem same (P : Prop) (h : P) : P := by sorry",
            {
                "k": "forall",
                "bi": "default",
                "dom": {"k": "sort", "u": "0"},
                "body": {
                    "k": "forall",
                    "bi": "default",
                    "dom": {"k": "bvar", "i": 0},
                    "body": {"k": "bvar", "i": 1},
                },
            },
        ),
        (
            "theorem implicit (P Q : Prop) {h : P} : Q := by sorry",
            _root(),
        ),
        (
            "theorem nested (P Q : Prop) (h : P) : Q → P := by sorry",
            _nested_root(),
        ),
    ],
)
def test_n12_rejects_identical_implicit_complex_and_nested_cases(
    source: str,
    root: dict[str, object],
) -> None:
    assert enumerate_n12_sites(source, operator_tree(root)) == ()


def test_n12_generation_has_exact_inverse_even_with_different_name_lengths() -> None:
    theorem, representation = _records(_SOURCE, "generate", _root())
    rule = N12ImplicationConverseRule(
        generation_config_hash="b" * 64,
        candidate_pool="fixture",
    )
    draft = rule.generate(theorem, representation, seed=7)[0]
    assert draft.candidate_code == (
        "theorem n12 (Premise Goal : Prop) (h : Goal) : Premise := by sorry"
    )
    assert draft.intended_relation == IntendedRelation.NEAR_MISS
    assert draft.intended_error_types == ("E26", "E30")
    assert draft.inverse_trace is not None
    assert apply_n12_trace(draft.candidate_code, draft.inverse_trace) == _SOURCE
    assert draft.metadata["resolved_semantic_label"] is False
    assert draft.metadata["training_eligible"] is False


def test_n12_structural_certificate_accepts_only_exact_converse() -> None:
    certificate = certify_implication_converse(_root(), _root(converse=True), 2)
    assert certificate.hypothesis_outer_index == 2
    assert certificate.source_root_hash != certificate.candidate_root_hash

    corrupted = _root(converse=True)
    assert isinstance(corrupted["body"], dict)
    assert isinstance(corrupted["body"]["body"], dict)
    corrupted["body"]["body"]["body"] = {"k": "bvar", "i": 1}
    with pytest.raises(N12ImplicationConverseError, match="not_exact_root_converse"):
        certify_implication_converse(_root(), corrupted, 2)


def test_n12_clean_audit_is_provisional_without_semantic_credit() -> None:
    theorem, representation = _records(_SOURCE, "audit", _root())
    rule = N12ImplicationConverseRule(
        generation_config_hash="b" * 64,
        candidate_pool="fixture",
    )
    draft = rule.generate(theorem, representation, seed=7)[0]
    candidate, candidate_representation = _candidate_records(
        theorem,
        representation,
        draft.candidate_code,
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
    assert audit.structural_diff_ok is True
    assert audit.inverse_or_roundtrip_ok is True
    assert audit.metadata["evidence_class"] == "D0"
    assert audit.metadata["failed_proof_search_used"] is False
    assert audit.metadata["resolved_semantic_label"] is False
    assert audit.metadata["training_eligible"] is False


def test_n12_audit_quarantines_structural_corruption() -> None:
    theorem, representation = _records(_SOURCE, "corrupt", _root())
    rule = N12ImplicationConverseRule(
        generation_config_hash="b" * 64,
        candidate_pool="fixture",
    )
    draft = rule.generate(theorem, representation, seed=7)[0]
    candidate, candidate_representation = _candidate_records(
        theorem,
        representation,
        draft.candidate_code,
    )
    corrupted = candidate_representation.model_copy(
        update={"operator_tree": operator_tree(_root())}
    )
    audit = rule.audit(theorem, representation, candidate, corrupted, draft)
    assert audit.recommended_quality_tier == QualityTier.UNKNOWN
    assert "candidate_not_exact_root_converse" in audit.violation_codes


def test_n12_trace_corruption_fails_closed() -> None:
    theorem, representation = _records(_SOURCE, "trace", _root())
    rule = N12ImplicationConverseRule(
        generation_config_hash="b" * 64,
        candidate_pool="fixture",
    )
    draft = rule.generate(theorem, representation, seed=7)[0]
    step = {**draft.transformation_trace[0], "left_text": "Wrong"}
    with pytest.raises(N12ImplicationConverseError, match="expected_text_mismatch"):
        apply_n12_trace(_SOURCE, (step,))


def test_n12_profile_binds_portfolio_and_dispatches_only_n12() -> None:
    loaded = load_v2_d0_n12_execution_config()
    assert loaded.config.profile_id == "deterministic_v2_d0_n12_experimental"
    assert loaded.config.active_rules[0].intended_error_types == ("E26", "E30")
    assert loaded.config.resolved_label_count == 0
    assert loaded.config.promoted_item_count == 0
    assert loaded.config.training_eligible is False

    theorem, representation = _records(_SOURCE, "runtime", _root())
    runtime = build_v2_d0_n12_runtime()
    execution = runtime.execute(
        "n12_implication_converse",
        theorem,
        representation,
        seed=7,
    )
    assert execution.attempt.terminal_outcome == "generated"
    assert len(execution.drafts) == 1
    with pytest.raises(V2D0N12ExecutionError, match="outside the N12 profile"):
        runtime.execute("n11_bound_variable_substitution", theorem, representation, seed=7)


def test_n12_persisted_scale_binds_profile_and_rule(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import leanfaith.transforms.v2_d0_scale as scale_module

    theorem, representation = _records(_SOURCE, "scale", _root())
    theorem_path = tmp_path / "theorems.jsonl"
    representation_path = tmp_path / "representations.jsonl"
    theorem_path.write_text(theorem.model_dump_json() + "\n", encoding="utf-8")
    representation_path.write_text(
        representation.model_dump_json() + "\n",
        encoding="utf-8",
    )

    def fake_build(
        backend: object,
        inputs: list[TheoremForRepresentation],
        **kwargs: object,
    ) -> list[RepresentationRecord]:
        del backend, kwargs
        assert len(inputs) == 1
        item = inputs[0]
        candidate_root = _root(converse=True)
        return [
            representation.model_copy(
                update={
                    "representation_id": make_id("repr", {"n12_scale_candidate": item.theorem_id}),
                    "theorem_id": item.theorem_id,
                    "raw_proof_stripped": item.proof_stripped,
                    "signature_explicit": "∀ (Premise Goal : Prop), Goal → Premise",
                    "semantic_atoms": semantic_atoms(candidate_root),
                    "operator_tree": operator_tree(candidate_root),
                    "alpha_identity_fingerprint": alpha_identity_fingerprint(candidate_root),
                }
            )
        ]

    monkeypatch.setattr(scale_module, "build_representations", fake_build)
    backend = _BatchBackend((LeanStatus.VALID_WITH_SORRY,))
    artifacts = run_v2_d0_scale(
        backend=cast(LeanInteractBackend, backend),
        runtime=build_v2_d0_n12_runtime(),
        theorem_path=theorem_path,
        representation_path=representation_path,
        project_dir=tmp_path,
        import_header="import LeanFaithFixtures",
        output_dir=tmp_path / "run",
        batch_size=1,
        base_seed=13,
    )
    assert artifacts.result_count == 1
    spec = artifacts.run_spec_path.read_text(encoding="utf-8")
    assert '"profile_id":"deterministic_v2_d0_n12_experimental"' in spec
    assert '"rule_id":"n12_implication_converse"' in spec
    result = artifacts.results_path.read_text(encoding="utf-8")
    assert '"terminal_status":"provisional_variant"' in result
    assert '"training_eligible":false' in result

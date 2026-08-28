"""Focused fail-closed tests for the provisional P13 E1 eta rule."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from leanfaith.representations import alpha_identity_fingerprint
from leanfaith.schemas import CANONICAL_VIEW_NAMES, ViewStatus, make_id
from leanfaith.schemas.enums import IntendedRelation, QualityTier, ValidationStatus
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms.materialize import build_derived_theorem_record
from leanfaith.transforms.positives.p13_restricted_eta import (
    P13RestrictedEtaError,
    P13RestrictedEtaRule,
    apply_eta_trace,
    enumerate_p13_sites,
)
from tests.unit.record_factories import representation_record, theorem_record

_SOURCE = "theorem eta (f : Nat → Bool) (g : Nat → Bool) : (fun (x : Nat) => f x) = g := by sorry"


def _tree() -> dict[str, object]:
    return {
        "root": {
            "k": "app",
            "fn": {"k": "const", "n": "Eq"},
            "arg": {"k": "bvar", "i": 0},
        }
    }


def _records(source: str, key: str) -> tuple[TheoremRecord, RepresentationRecord]:
    theorem_id = make_id("thm", {"p13": key})
    ancestry_id = make_id("anc", {"p13": key})
    theorem = theorem_record(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        declaration_name=key,
        declaration_full_name=key,
        proof_stripped_declaration=source,
        inline_elaboration_source="import LeanFaithFixtures\n" + source,
        statement_content_hash=hashlib.sha256(source.encode()).hexdigest(),
    )
    tree = _tree()
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
        representation_id=make_id("repr", {"p13": key}),
        theorem_id=theorem_id,
        raw_proof_stripped=source,
        headless=source.split(":", 1)[-1].split(":=", 1)[0],
        signature_pp="fixture eta type",
        signature_explicit="fixture explicit eta type",
        semantic_atoms=("const:Eq",),
        operator_tree=tree,
        alpha_identity_fingerprint=alpha_identity_fingerprint(tree),
        view_status=statuses,
    )
    return theorem, representation


def _candidate_records(
    source: TheoremRecord,
    source_representation: RepresentationRecord,
    candidate_code: str,
) -> tuple[TheoremRecord, RepresentationRecord]:
    rule = P13RestrictedEtaRule(generation_config_hash="a" * 64, candidate_pool="fixture")
    draft = rule.generate(source, source_representation, seed=7)[0]
    assert draft.candidate_code == candidate_code
    candidate = build_derived_theorem_record(
        draft=draft,
        sources=(source,),
        primary_source_id=source.theorem_id,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        inline_elaboration_source="import LeanFaithFixtures\n" + candidate_code,
    )
    candidate_representation = source_representation.model_copy(
        update={
            "representation_id": make_id("repr", {"p13_candidate": candidate.theorem_id}),
            "theorem_id": candidate.theorem_id,
            "raw_proof_stripped": candidate_code,
        }
    )
    return candidate, candidate_representation


def test_p13_contracts_exactly_one_explicit_eta_redex_and_replays_inverse() -> None:
    sites = enumerate_p13_sites(_SOURCE)
    assert len(sites) == 1
    site = sites[0]
    assert site.operation == "contract_explicit_nondependent_eta"
    assert dict(site.metadata) == {
        "binder_kind": "explicit",
        "codomain": "Bool",
        "domain": "Nat",
        "eta_argument": "x",
        "eta_binder": "x",
        "function_head": "f",
        "free_variable_absent": "true",
        "function_dependency": "nondependent_arrow",
    }

    theorem, representation = _records(_SOURCE, "exact")
    rule = P13RestrictedEtaRule(generation_config_hash="a" * 64, candidate_pool="fixture")
    drafts = rule.generate(theorem, representation, seed=7)
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.candidate_code == (
        "theorem eta (f : Nat → Bool) (g : Nat → Bool) : f = g := by sorry"
    )
    assert len(draft.transformation_trace) == 1
    assert draft.inverse_trace is not None and len(draft.inverse_trace) == 1
    assert apply_eta_trace(draft.candidate_code, draft.inverse_trace) == _SOURCE
    assert draft.expected_structural_diff["evidence_class"] == "E1"
    assert draft.expected_structural_diff["eta_step_count"] == 1
    assert draft.intended_relation == IntendedRelation.EQUIVALENT
    assert draft.metadata == {"generation_intention_only": True}


@pytest.mark.parametrize(
    "source",
    [
        # Implicit and instance function binders are outside P13.
        "theorem implicit {f : Nat → Bool} : (fun (x : Nat) => f x) = f := by sorry",
        "theorem instance [f : Nat → Bool] : (fun (x : Nat) => f x) = f := by sorry",
        # A dependent function binder is not the simple A -> B certificate.
        (
            "theorem dependent (f : (n : Nat) → Fin (n + 1)) : "
            "(fun (x : Nat) => f x) = f := by sorry"
        ),
        # Domain mismatch invalidates the local function-type certificate.
        "theorem mismatch (f : Nat → Bool) : (fun (x : Int) => f x) = f := by sorry",
        # The body is not the exact eta redex f x.
        "theorem extra (f : Nat → Nat → Bool) : (fun (x : Nat) => f x x) = f := by sorry",
        # Nested function expressions are intentionally unsupported.
        (
            "theorem nested (f : Nat → Nat → Bool) (g : Nat → Nat) : "
            "(fun (x : Nat) => f (g x) x) = f := by sorry"
        ),
        # A nested type ascription is not a declaration-header binder certificate.
        ("theorem ascribed (h : (id : Nat → Nat) = id) : (fun (x : Nat) => id x) = id := by sorry"),
    ],
)
def test_p13_rejects_implicit_instance_dependent_and_nonexact_forms(source: str) -> None:
    assert enumerate_p13_sites(source) == ()


@pytest.mark.parametrize(
    "source",
    [
        # The lambda binder occurs in the function position: no free-variable certificate.
        "theorem freevar (x : Nat → Bool) : (fun (x : Nat) => x x) = x := by sorry",
        # The redex argument differs from the lambda binder.
        ("theorem wrongarg (f : Nat → Bool) (y : Nat) : (fun (x : Nat) => f y) = f := by sorry"),
        # Local shadowing would make contraction/capture reasoning ambiguous.
        ("theorem shadow (f : Nat → Bool) : (fun (f : Nat) => f f) = f := by sorry"),
    ],
)
def test_p13_fails_closed_without_free_variable_and_capture_certificate(source: str) -> None:
    assert enumerate_p13_sites(source) == ()


def test_p13_rejects_multiple_eta_steps_instead_of_selecting_one() -> None:
    source = (
        "theorem twice (f : Nat → Bool) (g : Nat → Bool) : "
        "(fun (x : Nat) => f x) = (fun (y : Nat) => g y) := by sorry"
    )
    theorem, representation = _records(source, "twice")
    assert len(enumerate_p13_sites(source)) == 2
    applicability = P13RestrictedEtaRule(
        generation_config_hash="b" * 64,
        candidate_pool="fixture",
    ).assess(theorem, representation)
    assert not applicability.applicable
    assert "eta_redex_not_unique" in applicability.reason_codes


def test_p13_clean_audit_is_e1_provisional_and_never_training_eligible() -> None:
    theorem, representation = _records(_SOURCE, "audit")
    rule = P13RestrictedEtaRule(generation_config_hash="a" * 64, candidate_pool="fixture")
    draft = rule.generate(theorem, representation, seed=7)[0]
    candidate, candidate_representation = _candidate_records(
        theorem,
        representation,
        "theorem eta (f : Nat → Bool) (g : Nat → Bool) : f = g := by sorry",
    )
    audit = rule.audit(
        theorem,
        representation,
        candidate,
        candidate_representation,
        draft,
    )
    assert audit.violation_codes == ()
    assert audit.recommended_validation_status == ValidationStatus.ELABORATES_WITH_PLACEHOLDER
    assert audit.recommended_quality_tier == QualityTier.PROVISIONAL
    assert audit.structural_diff_ok is True
    assert audit.atom_mapping_ok is None
    assert audit.metadata["evidence_class"] == "E1"
    assert audit.metadata["free_variable_certificate_ok"] is True
    assert audit.metadata["general_reduction_invoked"] is False
    assert audit.metadata["resolved_semantic_label"] is False
    assert audit.metadata["training_eligible"] is False


def test_p13_audit_quarantines_corrupted_certificate_and_capture_trace() -> None:
    theorem, representation = _records(_SOURCE, "corrupt")
    rule = P13RestrictedEtaRule(generation_config_hash="a" * 64, candidate_pool="fixture")
    draft = rule.generate(theorem, representation, seed=7)[0]
    candidate, candidate_representation = _candidate_records(
        theorem,
        representation,
        "theorem eta (f : Nat → Bool) (g : Nat → Bool) : f = g := by sorry",
    )

    corrupt_diff = dict(draft.expected_structural_diff)
    corrupt_diff["free_variable_absent"] = "false"
    corrupt_certificate = draft.model_copy(update={"expected_structural_diff": corrupt_diff})
    rejected_certificate = rule.audit(
        theorem,
        representation,
        candidate,
        candidate_representation,
        corrupt_certificate,
    )
    assert "draft_id_mismatch" in rejected_certificate.violation_codes
    assert "eta_certificate_mismatch" in rejected_certificate.violation_codes
    assert rejected_certificate.recommended_validation_status == ValidationStatus.QUARANTINED
    assert rejected_certificate.recommended_quality_tier == QualityTier.UNKNOWN

    corrupt_inverse = tuple(dict(item) for item in draft.inverse_trace or ())
    corrupt_inverse[0]["replacement_text"] = "(fun (f : Nat) => f f)"
    capture_trace = draft.model_copy(update={"inverse_trace": corrupt_inverse})
    rejected_capture = rule.audit(
        theorem,
        representation,
        candidate,
        candidate_representation,
        capture_trace,
    )
    assert "draft_id_mismatch" in rejected_capture.violation_codes
    assert "eta_certificate_mismatch" in rejected_capture.violation_codes
    assert "inverse_replay_failed" in rejected_capture.violation_codes


def test_p13_trace_corruption_fails_closed() -> None:
    theorem, representation = _records(_SOURCE, "trace")
    draft = P13RestrictedEtaRule(
        generation_config_hash="c" * 64,
        candidate_pool="fixture",
    ).generate(theorem, representation, seed=3)[0]
    assert draft.inverse_trace is not None
    corrupt = tuple(dict(item) for item in draft.inverse_trace)
    corrupt[0]["start"] = 0
    with pytest.raises(P13RestrictedEtaError, match="expected text mismatch"):
        apply_eta_trace(draft.candidate_code, corrupt)


def test_p13_module_has_no_runtime_registration_or_broad_reduction_calls() -> None:
    module_path = (
        Path(__file__).parents[2]
        / "src"
        / "leanfaith"
        / "transforms"
        / "positives"
        / "p13_restricted_eta.py"
    )
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    inherited_names = {
        base.id
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        for base in node.bases
        if isinstance(base, ast.Name)
    }
    assert not any(module.startswith("leanfaith.lean") for module in imported_modules)
    assert not any("runtime" in module or "registry" in module for module in imported_modules)
    assert called_names.isdisjoint({"simp", "aesop", "isDefEq", "native_decide"})
    assert "_E0PresentationRule" not in inherited_names

"""Focused fail-closed tests for N18 v1.0 root equality polarity."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast

import pytest

from leanfaith.config.hashing import hash_file
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.lean.protocol import LeanStatus
from leanfaith.representations import TheoremForRepresentation, alpha_identity_fingerprint
from leanfaith.representations.atoms import operator_tree, semantic_atoms
from leanfaith.schemas import CANONICAL_VIEW_NAMES, RepresentationRecord, TheoremRecord
from leanfaith.schemas.enums import IntendedRelation, QualityTier, ValidationStatus, ViewStatus
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.variant import VariantDraft
from leanfaith.transforms.materialize import build_derived_theorem_record
from leanfaith.transforms.negatives.n18_equality_polarity import (
    N18EqualityPolarityError,
    N18EqualityPolarityRule,
    apply_n18_trace,
    build_equality_polarity_root,
    certify_equality_polarity,
    enumerate_n18_sites,
)
from leanfaith.transforms.protocol import expected_variant_draft_id
from leanfaith.transforms.v2_d0_n18_runtime import (
    V2D0N18ExecutionError,
    build_v2_d0_n18_runtime,
    load_v2_d0_n18_execution_config,
)
from leanfaith.transforms.v2_d0_scale_run import run_v2_d0_scale
from tests.unit.record_factories import representation_record, theorem_record
from tests.unit.test_deterministic_v2_n11_scale import _BatchBackend

_SOURCE_EQ = "theorem n18 (x y : Nat) : x = y := by sorry"
_SOURCE_NE = "theorem n18 (x y : Nat) : x ≠ y := by sorry"


def _nat() -> dict[str, object]:
    return {"k": "const", "n": "Nat", "us": "[]"}


def _app(function: dict[str, object], argument: dict[str, object]) -> dict[str, object]:
    return {"k": "app", "fn": function, "arg": argument}


def _eq(*, identical: bool = False) -> dict[str, object]:
    head: dict[str, object] = {"k": "const", "n": "Eq", "us": "[0]"}
    left = {"k": "bvar", "i": 1}
    right = left if identical else {"k": "bvar", "i": 0}
    return _app(_app(_app(head, _nat()), left), right)


def _root(*, negative: bool = False, identical: bool = False) -> dict[str, object]:
    conclusion = _eq(identical=identical)
    if negative:
        cursor = conclusion
        while cursor["k"] == "app":
            cursor = cast(dict[str, object], cursor["fn"])
        cursor["n"] = "Ne"
    return {
        "k": "forall",
        "bi": "default",
        "dom": _nat(),
        "body": {
            "k": "forall",
            "bi": "default",
            "dom": _nat(),
            "body": conclusion,
        },
    }


def _records(
    source: str,
    key: str,
    root: dict[str, object],
) -> tuple[TheoremRecord, RepresentationRecord]:
    theorem_id = make_id("thm", {"n18": key})
    ancestry_id = make_id("anc", {"n18": key})
    theorem = theorem_record(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        declaration_name="n18",
        declaration_full_name="n18",
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
        representation_id=make_id("repr", {"n18": key}),
        theorem_id=theorem_id,
        raw_proof_stripped=source,
        headless="(x y : Nat) : x ≠ y" if "≠" in source else "(x y : Nat) : x = y",
        signature_pp="∀ (x y : Nat), x ≠ y" if "≠" in source else "∀ (x y : Nat), x = y",
        signature_explicit=("∀ (x y : Nat), Ne x y" if "≠" in source else "∀ (x y : Nat), Eq x y"),
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
    negative: bool,
) -> tuple[TheoremRecord, RepresentationRecord]:
    candidate = build_derived_theorem_record(
        draft=draft,
        sources=(source,),
        primary_source_id=source.theorem_id,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        inline_elaboration_source="import LeanFaithFixtures\n" + draft.candidate_code,
    )
    candidate_root = _root(negative=negative)
    candidate_representation = source_representation.model_copy(
        update={
            "representation_id": make_id("repr", {"n18_candidate": candidate.theorem_id}),
            "theorem_id": candidate.theorem_id,
            "raw_proof_stripped": draft.candidate_code,
            "headless": "(x y : Nat) : x ≠ y" if negative else "(x y : Nat) : x = y",
            "signature_pp": "∀ (x y : Nat), x ≠ y" if negative else "∀ (x y : Nat), x = y",
            "signature_explicit": (
                "∀ (x y : Nat), Ne x y" if negative else "∀ (x y : Nat), Eq x y"
            ),
            "semantic_atoms": semantic_atoms(candidate_root),
            "operator_tree": operator_tree(candidate_root),
            "alpha_identity_fingerprint": alpha_identity_fingerprint(candidate_root),
        }
    )
    return candidate, candidate_representation


def test_n18_enumerates_both_root_directions_and_exact_trees() -> None:
    (eq_site,) = enumerate_n18_sites(_SOURCE_EQ, operator_tree(_root()))
    assert eq_site.direction == "eq_to_ne"
    assert (eq_site.source_operator, eq_site.candidate_operator) == ("=", "≠")
    assert build_equality_polarity_root(_root(), 2) == _root(negative=True)

    (ne_site,) = enumerate_n18_sites(_SOURCE_NE, operator_tree(_root(negative=True)))
    assert ne_site.direction == "ne_to_eq"
    assert (ne_site.source_operator, ne_site.candidate_operator) == ("≠", "=")
    assert build_equality_polarity_root(_root(negative=True), 2) == _root()


@pytest.mark.parametrize(
    "source",
    [
        "theorem n18 (x y : Nat) : x = x := by sorry",
        "theorem n18 (x y : Nat) : True → x = y := by sorry",
        "theorem n18 (x y : Nat) : x == y := by sorry",
        "theorem n18 (x y : Nat) : x != y := by sorry",
        "theorem n18 (x y : Nat) : (x = y) = True := by sorry",
        "theorem n18 (x y : Nat) : x = by exact y := by sorry",
        "theorem n18 (x y : Nat) : x ≠ (match y with | z => z) := by sorry",
        'theorem n18 (x y : Nat) : x = "unsafe" := by sorry',
        "theorem n18 (x y : Nat) : x = y -- unsafe\n:= by sorry",
    ],
)
def test_n18_rejects_nonroot_ambiguous_or_scoped_surfaces(source: str) -> None:
    assert enumerate_n18_sites(source, operator_tree(_root())) == ()


def test_n18_rejects_surface_tree_mismatch_and_identical_operands() -> None:
    assert enumerate_n18_sites(_SOURCE_EQ, operator_tree(_root(negative=True))) == ()
    assert enumerate_n18_sites(_SOURCE_NE, operator_tree(_root())) == ()
    assert enumerate_n18_sites(_SOURCE_EQ, operator_tree(_root(identical=True))) == ()


@pytest.mark.parametrize(
    ("source", "candidate", "negative_source"),
    [
        (_SOURCE_EQ, _SOURCE_NE, False),
        (_SOURCE_NE, _SOURCE_EQ, True),
    ],
)
def test_n18_generation_is_invertible_and_unresolved(
    source: str,
    candidate: str,
    negative_source: bool,
) -> None:
    theorem, representation = _records(
        source, f"generate:{negative_source}", _root(negative=negative_source)
    )
    rule = N18EqualityPolarityRule(generation_config_hash="a" * 64, candidate_pool="fixture")
    (draft,) = rule.generate(theorem, representation, seed=18)
    assert draft.candidate_code == candidate
    assert draft.intended_relation == IntendedRelation.NEAR_MISS
    assert draft.intended_error_types == ("E10", "E26")
    assert draft.inverse_trace is not None
    assert apply_n18_trace(draft.candidate_code, draft.inverse_trace) == source
    assert draft.metadata["near_miss"] is True
    assert draft.metadata["resolved_semantic_label"] is False
    assert draft.metadata["training_eligible"] is False


def test_n18_certificate_and_semantic_atom_audit() -> None:
    certificate = certify_equality_polarity(_root(), _root(negative=True), 2)
    assert certificate.direction == "eq_to_ne"
    assert certificate.source_atoms_hash != certificate.candidate_atoms_hash
    with pytest.raises(N18EqualityPolarityError, match="candidate_not_exact"):
        certify_equality_polarity(_root(), _root(), 2)

    theorem, representation = _records(_SOURCE_EQ, "audit", _root())
    rule = N18EqualityPolarityRule(generation_config_hash="b" * 64, candidate_pool="fixture")
    (draft,) = rule.generate(theorem, representation, seed=18)
    candidate, candidate_representation = _candidate_records(
        theorem, representation, draft, negative=True
    )
    audit = rule.audit(
        theorem,
        representation,
        candidate,
        candidate_representation,
        draft,
    )
    assert audit.violation_codes == ()
    assert audit.structural_diff_ok is True
    assert audit.atom_mapping_ok is True
    assert audit.inverse_or_roundtrip_ok is True
    assert audit.recommended_quality_tier == QualityTier.PROVISIONAL
    assert audit.metadata["evidence_class"] == "D0"
    assert audit.metadata["failed_proof_search_used"] is False
    assert audit.metadata["resolved_semantic_label"] is False
    assert audit.metadata["training_eligible"] is False


def test_n18_tampered_candidate_atoms_are_quarantined() -> None:
    theorem, representation = _records(_SOURCE_EQ, "tamper", _root())
    rule = N18EqualityPolarityRule(generation_config_hash="c" * 64, candidate_pool="fixture")
    (draft,) = rule.generate(theorem, representation, seed=18)
    candidate, candidate_representation = _candidate_records(
        theorem, representation, draft, negative=True
    )
    audit = rule.audit(
        theorem,
        representation,
        candidate,
        candidate_representation.model_copy(update={"semantic_atoms": ("tampered",)}),
        draft,
    )
    assert "candidate_alpha_or_semantic_audit_mismatch" in audit.violation_codes
    assert audit.recommended_quality_tier == QualityTier.UNKNOWN


@pytest.mark.parametrize(
    ("updates", "expected_violation"),
    [
        (
            {"intended_relation": IntendedRelation.EQUIVALENT},
            "draft_semantic_intention_mismatch",
        ),
        (
            {"intended_error_types": ("E01",)},
            "draft_semantic_intention_mismatch",
        ),
        (
            {"candidate_pool": "tampered_pool"},
            "draft_candidate_pool_mismatch",
        ),
        (
            {
                "metadata": {
                    "generation_intention_only": False,
                    "near_miss": True,
                    "resolved_semantic_label": False,
                    "structural_direction": "eq_to_ne",
                    "training_eligible": False,
                }
            },
            "draft_fixed_metadata_mismatch",
        ),
        (
            {
                "metadata": {
                    "generation_intention_only": True,
                    "near_miss": True,
                    "resolved_semantic_label": False,
                    "structural_direction": "ne_to_eq",
                    "training_eligible": False,
                }
            },
            "draft_fixed_metadata_mismatch",
        ),
        (
            {"expected_atom_mapping": {"const:Eq": "const:Ne"}},
            "draft_expected_atom_mapping_mismatch",
        ),
        (
            {
                "formalrx_sci_requested": "S1.1",
                "formalrx_sci_validated": "S1.1",
                "formalrx_sci_validation_status": "validated",
                "formalrx_sci_proposer_family": "proposer",
                "formalrx_sci_validator_family": "validator",
            },
            "draft_sci_provenance_mismatch",
        ),
    ],
)
def test_n18_audit_rejects_self_consistently_reidentified_policy_tampering(
    updates: dict[str, object],
    expected_violation: str,
) -> None:
    theorem, representation = _records(_SOURCE_EQ, f"draft-tamper:{expected_violation}", _root())
    rule = N18EqualityPolarityRule(generation_config_hash="f" * 64, candidate_pool="fixture")
    (draft,) = rule.generate(theorem, representation, seed=18)
    tampered = draft.model_copy(update=updates)
    tampered = tampered.model_copy(update={"draft_id": expected_variant_draft_id(tampered)})
    candidate, candidate_representation = _candidate_records(
        theorem, representation, tampered, negative=True
    )
    audit = rule.audit(
        theorem,
        representation,
        candidate,
        candidate_representation,
        tampered,
    )
    assert expected_violation in audit.violation_codes
    assert audit.recommended_quality_tier == QualityTier.UNKNOWN


def test_n18_runtime_is_versioned_and_preserves_old_config_bytes() -> None:
    loaded = load_v2_d0_n18_execution_config()
    assert loaded.config.profile_id == "deterministic_v2_d0_n18_experimental"
    assert loaded.config.active_rules[0].intended_error_types == ("E10", "E26")
    assert loaded.config.resolved_label_count == 0
    assert loaded.config.promoted_item_count == 0
    assert loaded.config.training_eligible is False

    theorem, representation = _records(_SOURCE_EQ, "runtime", _root())
    runtime = build_v2_d0_n18_runtime()
    execution = runtime.execute("n18_root_equality_polarity", theorem, representation, seed=18)
    assert execution.attempt.terminal_outcome == "generated"
    assert len(execution.drafts) == 1
    with pytest.raises(V2D0N18ExecutionError, match="outside the N18 profile"):
        runtime.execute("n17_role_sensitive_arguments", theorem, representation, seed=18)

    expected = {
        "configs/transformations/v2.yaml": (
            "d8d71c0a77bd7afb3f365e40d9ca3ad8c2d989e1d16cab4c4d462da9c14ac487"
        ),
        "configs/transformations/v2_d0_n17_experimental.yaml": (
            "e4f6b5d87917bc39e3a51254c5bf17ecb26980880e899df90c8113b771cccb5d"
        ),
    }
    assert {path: hash_file(Path(path)) for path in expected} == expected


def test_n18_persisted_scale_binds_profile_and_rule(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import leanfaith.transforms.v2_d0_scale as scale_module

    theorem, representation = _records(_SOURCE_EQ, "scale", _root())
    theorem_path = tmp_path / "theorems.jsonl"
    representation_path = tmp_path / "representations.jsonl"
    theorem_path.write_text(theorem.model_dump_json() + "\n", encoding="utf-8")
    representation_path.write_text(representation.model_dump_json() + "\n", encoding="utf-8")

    def fake_build(
        backend: object,
        inputs: list[TheoremForRepresentation],
        **kwargs: object,
    ) -> list[RepresentationRecord]:
        del backend, kwargs
        assert len(inputs) == 1
        item = inputs[0]
        candidate_root = _root(negative=True)
        return [
            representation.model_copy(
                update={
                    "representation_id": make_id("repr", {"n18_scale_candidate": item.theorem_id}),
                    "theorem_id": item.theorem_id,
                    "raw_proof_stripped": item.proof_stripped,
                    "headless": "(x y : Nat) : x ≠ y",
                    "signature_pp": "∀ (x y : Nat), x ≠ y",
                    "signature_explicit": "∀ (x y : Nat), Ne x y",
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
        runtime=build_v2_d0_n18_runtime(),
        theorem_path=theorem_path,
        representation_path=representation_path,
        project_dir=tmp_path,
        import_header="import LeanFaithFixtures",
        output_dir=tmp_path / "run",
        batch_size=1,
        base_seed=18,
    )
    assert artifacts.result_count == 1
    spec = artifacts.run_spec_path.read_text(encoding="utf-8")
    assert '"profile_id":"deterministic_v2_d0_n18_experimental"' in spec
    assert '"rule_id":"n18_root_equality_polarity"' in spec
    result = artifacts.results_path.read_text(encoding="utf-8")
    assert '"terminal_status":"provisional_variant"' in result
    assert '"training_eligible":false' in result


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("x", "y"),
        ("x + 1", "Nat.succ y"),
        ("f x", "g y"),
        ("(x, y)", "(y, x)"),
    ],
)
def test_n18_complete_operand_surface_property(left: str, right: str) -> None:
    source = f"theorem n18 (x y : Nat) : ({left}) = ({right}) := by sorry"
    theorem, representation = _records(source, f"property:{left}:{right}", _root())
    rule = N18EqualityPolarityRule(generation_config_hash="d" * 64, candidate_pool="fixture")
    (draft,) = rule.generate(theorem, representation, seed=18)
    assert draft.candidate_code == source.replace(" = ", " ≠ ", 1)
    assert draft.inverse_trace is not None
    assert apply_n18_trace(draft.candidate_code, draft.inverse_trace) == source

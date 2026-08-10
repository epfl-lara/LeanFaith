"""Focused LF-034 tests for the provisional N13 D0 family."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast

import pytest

from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.lean.protocol import LeanStatus
from leanfaith.representations import TheoremForRepresentation, alpha_identity_fingerprint
from leanfaith.representations.atoms import operator_tree, semantic_atoms
from leanfaith.schemas import CANONICAL_VIEW_NAMES, RepresentationRecord, TheoremRecord
from leanfaith.schemas.enums import IntendedRelation, QualityTier, ValidationStatus, ViewStatus
from leanfaith.schemas.ids import make_id
from leanfaith.transforms.materialize import build_derived_theorem_record
from leanfaith.transforms.negatives.n13_witness_dependency import (
    N13WitnessDependencyError,
    N13WitnessDependencyRule,
    apply_n13_trace,
    build_witness_dependency_root,
    certify_witness_dependency,
    enumerate_n13_sites,
)
from leanfaith.transforms.v2_d0_n13_runtime import (
    V2D0N13ExecutionError,
    build_v2_d0_n13_runtime,
    load_v2_d0_n13_execution_config,
)
from leanfaith.transforms.v2_d0_scale_run import run_v2_d0_scale
from tests.unit.record_factories import representation_record, theorem_record
from tests.unit.test_deterministic_v2_n11_scale import _BatchBackend

_SOURCE = "theorem n13 : ∀ x : Nat, ∃ y : Nat, x = y := by sorry"


def _nat() -> dict[str, object]:
    return {"k": "const", "n": "Nat", "us": "[]"}


def _equality(left: int, right: int) -> dict[str, object]:
    return {
        "k": "app",
        "fn": {
            "k": "app",
            "fn": {
                "k": "app",
                "fn": {"k": "const", "n": "Eq", "us": "[0]"},
                "arg": _nat(),
            },
            "arg": {"k": "bvar", "i": left},
        },
        "arg": {"k": "bvar", "i": right},
    }


def _root(*, swapped: bool = False) -> dict[str, object]:
    exists: dict[str, object] = {
        "k": "app",
        "fn": {
            "k": "app",
            "fn": {"k": "const", "n": "Exists", "us": "[1]"},
            "arg": _nat(),
        },
        "arg": {
            "k": "lam",
            "bi": "default",
            "dom": _nat(),
            "body": (
                {
                    "k": "forall",
                    "bi": "default",
                    "dom": _nat(),
                    "body": _equality(0, 1),
                }
                if swapped
                else _equality(1, 0)
            ),
        },
    }
    if swapped:
        return exists
    return {
        "k": "forall",
        "bi": "default",
        "dom": _nat(),
        "body": exists,
    }


def _dependent_root() -> dict[str, object]:
    witness_type = {
        "k": "app",
        "fn": {"k": "const", "n": "Fin", "us": "[]"},
        "arg": {"k": "bvar", "i": 0},
    }
    return {
        "k": "forall",
        "bi": "default",
        "dom": _nat(),
        "body": {
            "k": "app",
            "fn": {
                "k": "app",
                "fn": {"k": "const", "n": "Exists", "us": "[1]"},
                "arg": witness_type,
            },
            "arg": {
                "k": "lam",
                "bi": "default",
                "dom": witness_type,
                "body": _equality(1, 0),
            },
        },
    }


def _records(
    source: str,
    key: str,
    root: dict[str, object],
) -> tuple[TheoremRecord, RepresentationRecord]:
    theorem_id = make_id("thm", {"n13": key})
    ancestry_id = make_id("anc", {"n13": key})
    theorem = theorem_record(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        declaration_name="n13",
        declaration_full_name="n13",
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
        representation_id=make_id("repr", {"n13": key}),
        theorem_id=theorem_id,
        raw_proof_stripped=source,
        headless="∀ x : Nat, ∃ y : Nat, x = y",
        signature_pp="∀ x : Nat, ∃ y : Nat, x = y",
        signature_explicit="∀ x : Nat, ∃ y : Nat, Eq x y",
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
    rule = N13WitnessDependencyRule(
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
    candidate_root = _root(swapped=True)
    candidate_representation = source_representation.model_copy(
        update={
            "representation_id": make_id("repr", {"n13_candidate": candidate.theorem_id}),
            "theorem_id": candidate.theorem_id,
            "raw_proof_stripped": draft_code,
            "signature_pp": "∃ y : Nat, ∀ x : Nat, x = y",
            "signature_explicit": "∃ y : Nat, ∀ x : Nat, Eq x y",
            "semantic_atoms": semantic_atoms(candidate_root),
            "operator_tree": operator_tree(candidate_root),
            "alpha_identity_fingerprint": alpha_identity_fingerprint(candidate_root),
        }
    )
    return candidate, candidate_representation


def test_n13_enumerates_exact_independent_witness_site() -> None:
    (site,) = enumerate_n13_sites(_SOURCE, operator_tree(_root()))
    assert site.universal_name == "x"
    assert site.witness_name == "y"
    assert site.header_binder_count == 0
    assert site.candidate_text == "∃ y : Nat, ∀ x : Nat, x = y"
    assert build_witness_dependency_root(_root(), 0) == _root(swapped=True)


def test_n13_rejects_dependent_witness_type() -> None:
    source = "theorem dependent : ∀ n : Nat, ∃ y : Fin n, y.val = n := by sorry"
    assert enumerate_n13_sites(source, operator_tree(_dependent_root())) == ()
    with pytest.raises(N13WitnessDependencyError, match="depends_on_universal"):
        build_witness_dependency_root(_dependent_root(), 0)


@pytest.mark.parametrize(
    "source",
    [
        "theorem implicit : ∀ {x : Nat}, ∃ y : Nat, x = y := by sorry",
        "theorem untyped : ∀ x, ∃ y : Nat, x = y := by sorry",
        "theorem reversed : ∃ y : Nat, ∀ x : Nat, x = y := by sorry",
    ],
)
def test_n13_rejects_out_of_scope_surface_forms(source: str) -> None:
    assert enumerate_n13_sites(source, operator_tree(_root())) == ()


def test_n13_generation_is_exactly_invertible_and_provisional() -> None:
    theorem, representation = _records(_SOURCE, "generate", _root())
    rule = N13WitnessDependencyRule(
        generation_config_hash="b" * 64,
        candidate_pool="fixture",
    )
    draft = rule.generate(theorem, representation, seed=7)[0]
    assert draft.candidate_code == ("theorem n13 : ∃ y : Nat, ∀ x : Nat, x = y := by sorry")
    assert draft.intended_relation == IntendedRelation.NEAR_MISS
    assert draft.intended_error_types == ("E05", "E22")
    assert draft.inverse_trace is not None
    assert apply_n13_trace(draft.candidate_code, draft.inverse_trace) == _SOURCE
    assert draft.metadata["resolved_semantic_label"] is False
    assert draft.metadata["training_eligible"] is False


def test_n13_structural_certificate_accepts_only_exact_permutation() -> None:
    certificate = certify_witness_dependency(_root(), _root(swapped=True), 0)
    assert certificate.source_root_hash != certificate.candidate_root_hash

    corrupted = _root(swapped=True)
    assert isinstance(corrupted["arg"], dict)
    assert isinstance(corrupted["arg"]["body"], dict)
    corrupted["arg"]["body"]["body"] = _equality(1, 0)
    with pytest.raises(N13WitnessDependencyError, match="not_exact"):
        certify_witness_dependency(_root(), corrupted, 0)


def test_n13_clean_audit_is_provisional_without_semantic_credit() -> None:
    theorem, representation = _records(_SOURCE, "audit", _root())
    rule = N13WitnessDependencyRule(
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


def test_n13_trace_corruption_fails_closed() -> None:
    theorem, representation = _records(_SOURCE, "trace", _root())
    rule = N13WitnessDependencyRule(
        generation_config_hash="b" * 64,
        candidate_pool="fixture",
    )
    draft = rule.generate(theorem, representation, seed=7)[0]
    corrupted = ({**draft.transformation_trace[0], "expected_text": "wrong"},)
    with pytest.raises(N13WitnessDependencyError, match="expected_text_mismatch"):
        apply_n13_trace(_SOURCE, corrupted)


def test_n13_profile_binds_portfolio_and_dispatches_only_n13() -> None:
    loaded = load_v2_d0_n13_execution_config()
    assert loaded.config.profile_id == "deterministic_v2_d0_n13_experimental"
    assert loaded.config.active_rules[0].intended_error_types == ("E05", "E22")
    assert loaded.config.resolved_label_count == 0
    assert loaded.config.promoted_item_count == 0
    assert loaded.config.training_eligible is False

    theorem, representation = _records(_SOURCE, "runtime", _root())
    runtime = build_v2_d0_n13_runtime()
    execution = runtime.execute(
        "n13_witness_dependency",
        theorem,
        representation,
        seed=7,
    )
    assert execution.attempt.terminal_outcome == "generated"
    assert len(execution.drafts) == 1
    with pytest.raises(V2D0N13ExecutionError, match="outside the N13 profile"):
        runtime.execute("n12_implication_converse", theorem, representation, seed=7)


def test_n13_persisted_scale_binds_profile_and_rule(
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
        candidate_root = _root(swapped=True)
        return [
            representation.model_copy(
                update={
                    "representation_id": make_id("repr", {"n13_scale_candidate": item.theorem_id}),
                    "theorem_id": item.theorem_id,
                    "raw_proof_stripped": item.proof_stripped,
                    "signature_pp": "∃ y : Nat, ∀ x : Nat, x = y",
                    "signature_explicit": "∃ y : Nat, ∀ x : Nat, Eq x y",
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
        runtime=build_v2_d0_n13_runtime(),
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
    assert '"profile_id":"deterministic_v2_d0_n13_experimental"' in spec
    assert '"rule_id":"n13_witness_dependency"' in spec
    result = artifacts.results_path.read_text(encoding="utf-8")
    assert '"terminal_status":"provisional_variant"' in result
    assert '"training_eligible":false' in result

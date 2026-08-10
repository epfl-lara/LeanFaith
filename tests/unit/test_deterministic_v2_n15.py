"""Focused LF-034 tests for the provisional N15 D0 family."""

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
from leanfaith.schemas.variant import VariantDraft
from leanfaith.transforms.materialize import build_derived_theorem_record
from leanfaith.transforms.negatives.n15_conjunct_omission import (
    N15ConjunctOmissionError,
    N15ConjunctOmissionRule,
    apply_n15_trace,
    build_conjunct_omission_root,
    certify_conjunct_omission,
    enumerate_n15_sites,
)
from leanfaith.transforms.v2_d0_n15_runtime import (
    V2D0N15ExecutionError,
    build_v2_d0_n15_runtime,
    load_v2_d0_n15_execution_config,
)
from leanfaith.transforms.v2_d0_scale_run import run_v2_d0_scale
from tests.unit.record_factories import representation_record, theorem_record
from tests.unit.test_deterministic_v2_n11_scale import _BatchBackend

_SOURCE = "theorem n15 (x : Nat) : x = 0 ∧ x = 1 := by sorry"


def _nat() -> dict[str, object]:
    return {"k": "const", "n": "Nat", "us": "[]"}


def _eq(value: int) -> dict[str, object]:
    return {
        "k": "app",
        "fn": {
            "k": "app",
            "fn": {
                "k": "app",
                "fn": {"k": "const", "n": "Eq", "us": "[0]"},
                "arg": _nat(),
            },
            "arg": {"k": "bvar", "i": 0},
        },
        "arg": {"k": "lit", "nat": str(value)},
    }


def _and(left: dict[str, object], right: dict[str, object]) -> dict[str, object]:
    return {
        "k": "app",
        "fn": {
            "k": "app",
            "fn": {"k": "const", "n": "And", "us": "[]"},
            "arg": left,
        },
        "arg": right,
    }


def _root(*, retained: str | None = None) -> dict[str, object]:
    body = _and(_eq(0), _eq(1))
    if retained == "left":
        body = _eq(0)
    elif retained == "right":
        body = _eq(1)
    return {
        "k": "forall",
        "bi": "default",
        "dom": _nat(),
        "body": body,
    }


def _records(
    source: str,
    key: str,
    root: dict[str, object],
) -> tuple[TheoremRecord, RepresentationRecord]:
    theorem_id = make_id("thm", {"n15": key})
    ancestry_id = make_id("anc", {"n15": key})
    theorem = theorem_record(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        declaration_name="n15",
        declaration_full_name="n15",
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
        representation_id=make_id("repr", {"n15": key}),
        theorem_id=theorem_id,
        raw_proof_stripped=source,
        headless="(x : Nat) : x = 0 ∧ x = 1",
        signature_pp="(x : Nat) : x = 0 ∧ x = 1",
        signature_explicit="(x : Nat) : And (Eq x 0) (Eq x 1)",
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
) -> tuple[TheoremRecord, RepresentationRecord]:
    retained_side = str(draft.metadata["retained_side"])
    candidate = build_derived_theorem_record(
        draft=draft,
        sources=(source,),
        primary_source_id=source.theorem_id,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        inline_elaboration_source="import LeanFaithFixtures\n" + draft.candidate_code,
    )
    candidate_root = _root(retained=retained_side)
    candidate_representation = source_representation.model_copy(
        update={
            "representation_id": make_id("repr", {"n15_candidate": candidate.theorem_id}),
            "theorem_id": candidate.theorem_id,
            "raw_proof_stripped": draft.candidate_code,
            "headless": f"(x : Nat) : x = {'0' if retained_side == 'left' else '1'}",
            "signature_pp": f"(x : Nat) : x = {'0' if retained_side == 'left' else '1'}",
            "signature_explicit": (f"(x : Nat) : Eq x {'0' if retained_side == 'left' else '1'}"),
            "semantic_atoms": semantic_atoms(candidate_root),
            "operator_tree": operator_tree(candidate_root),
            "alpha_identity_fingerprint": alpha_identity_fingerprint(candidate_root),
        }
    )
    return candidate, candidate_representation


def test_n15_enumerates_both_distinct_top_level_omissions() -> None:
    sites = enumerate_n15_sites(_SOURCE, operator_tree(_root()))
    assert {site.retained_side for site in sites} == {"left", "right"}
    by_side = {site.retained_side: site for site in sites}
    assert by_side["left"].candidate_text == "x = 0"
    assert by_side["right"].candidate_text == "x = 1"
    assert build_conjunct_omission_root(_root(), 1, "left") == _root(retained="left")
    assert build_conjunct_omission_root(_root(), 1, "right") == _root(retained="right")


def test_n15_seed_selection_is_deterministic_and_reaches_both_sides() -> None:
    theorem, representation = _records(_SOURCE, "seeds", _root())
    rule = N15ConjunctOmissionRule(
        generation_config_hash="d" * 64,
        candidate_pool="fixture",
    )
    first = rule.generate(theorem, representation, seed=15)[0]
    assert rule.generate(theorem, representation, seed=15)[0].draft_id == first.draft_id
    sides = {
        rule.generate(theorem, representation, seed=seed)[0].metadata["retained_side"]
        for seed in range(100)
    }
    assert sides == {"left", "right"}


def test_n15_rejects_duplicate_and_nested_conjuncts() -> None:
    duplicate = {
        **_root(),
        "body": _and(_eq(0), _eq(0)),
    }
    nested = {
        **_root(),
        "body": _and(_and(_eq(0), _eq(1)), _eq(0)),
    }
    with pytest.raises(N15ConjunctOmissionError, match="duplicate_conjuncts"):
        build_conjunct_omission_root(duplicate, 1, "left")
    with pytest.raises(N15ConjunctOmissionError, match="nested_conjunction"):
        build_conjunct_omission_root(nested, 1, "left")


def test_n15_generation_is_exactly_invertible_and_provisional() -> None:
    theorem, representation = _records(_SOURCE, "generate", _root())
    rule = N15ConjunctOmissionRule(
        generation_config_hash="d" * 64,
        candidate_pool="fixture",
    )
    draft = rule.generate(theorem, representation, seed=15)[0]
    assert draft.candidate_code in {
        "theorem n15 (x : Nat) : x = 0 := by sorry",
        "theorem n15 (x : Nat) : x = 1 := by sorry",
    }
    assert draft.intended_relation == IntendedRelation.NEAR_MISS
    assert draft.intended_error_types == ("E20", "E26")
    assert draft.inverse_trace is not None
    assert apply_n15_trace(draft.candidate_code, draft.inverse_trace) == _SOURCE
    assert draft.metadata["resolved_semantic_label"] is False
    assert draft.metadata["training_eligible"] is False


def test_n15_structural_certificate_accepts_only_selected_projection() -> None:
    left = certify_conjunct_omission(_root(), _root(retained="left"), 1, "left")
    assert left.retained_side == "left"
    with pytest.raises(N15ConjunctOmissionError, match="not_exact"):
        certify_conjunct_omission(_root(), _root(retained="right"), 1, "left")


def test_n15_clean_audit_is_provisional_without_semantic_credit() -> None:
    theorem, representation = _records(_SOURCE, "audit", _root())
    rule = N15ConjunctOmissionRule(
        generation_config_hash="d" * 64,
        candidate_pool="fixture",
    )
    draft = rule.generate(theorem, representation, seed=15)[0]
    candidate, candidate_representation = _candidate_records(
        theorem,
        representation,
        draft,
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


def test_n15_trace_corruption_fails_closed() -> None:
    theorem, representation = _records(_SOURCE, "trace", _root())
    rule = N15ConjunctOmissionRule(
        generation_config_hash="d" * 64,
        candidate_pool="fixture",
    )
    draft = rule.generate(theorem, representation, seed=15)[0]
    corrupted = ({**draft.transformation_trace[0], "expected_text": "wrong"},)
    with pytest.raises(N15ConjunctOmissionError, match="expected_text_mismatch"):
        apply_n15_trace(_SOURCE, corrupted)


def test_n15_profile_binds_portfolio_and_dispatches_only_n15() -> None:
    loaded = load_v2_d0_n15_execution_config()
    assert loaded.config.profile_id == "deterministic_v2_d0_n15_experimental"
    assert loaded.config.active_rules[0].intended_error_types == ("E20", "E26")
    assert loaded.config.resolved_label_count == 0
    assert loaded.config.promoted_item_count == 0
    assert loaded.config.training_eligible is False

    theorem, representation = _records(_SOURCE, "runtime", _root())
    runtime = build_v2_d0_n15_runtime()
    execution = runtime.execute("n15_conjunct_omission", theorem, representation, seed=15)
    assert execution.attempt.terminal_outcome == "generated"
    assert len(execution.drafts) == 1
    with pytest.raises(V2D0N15ExecutionError, match="outside the N15 profile"):
        runtime.execute("n14_negation_scope", theorem, representation, seed=15)


def test_n15_persisted_scale_binds_profile_and_rule(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import leanfaith.transforms.v2_d0_scale as scale_module

    theorem, representation = _records(_SOURCE, "scale", _root())
    runtime = build_v2_d0_n15_runtime()
    draft = runtime.execute("n15_conjunct_omission", theorem, representation, seed=15).drafts[0]
    retained_side = str(draft.metadata["retained_side"])
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
        candidate_root = _root(retained=retained_side)
        return [
            representation.model_copy(
                update={
                    "representation_id": make_id("repr", {"n15_scale_candidate": item.theorem_id}),
                    "theorem_id": item.theorem_id,
                    "raw_proof_stripped": item.proof_stripped,
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
        runtime=runtime,
        theorem_path=theorem_path,
        representation_path=representation_path,
        project_dir=tmp_path,
        import_header="import LeanFaithFixtures",
        output_dir=tmp_path / "run",
        batch_size=1,
        base_seed=15,
    )
    assert artifacts.result_count == 1
    spec = artifacts.run_spec_path.read_text(encoding="utf-8")
    assert '"profile_id":"deterministic_v2_d0_n15_experimental"' in spec
    assert '"rule_id":"n15_conjunct_omission"' in spec
    result = artifacts.results_path.read_text(encoding="utf-8")
    assert '"terminal_status":"provisional_variant"' in result
    assert '"training_eligible":false' in result

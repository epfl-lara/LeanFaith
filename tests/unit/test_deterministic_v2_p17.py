"""Focused LF-033 tests for the provisional P17 E2 family."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, cast

import pytest

from leanfaith.representations import alpha_identity_fingerprint
from leanfaith.representations.atoms import operator_tree, semantic_atoms
from leanfaith.schemas import CANONICAL_VIEW_NAMES, RepresentationRecord, TheoremRecord
from leanfaith.schemas.enums import IntendedRelation, QualityTier, ValidationStatus, ViewStatus
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.variant import VariantDraft
from leanfaith.transforms.materialize import build_derived_theorem_record
from leanfaith.transforms.positives.p17_hypothesis_packing import (
    P17HypothesisPackingError,
    P17HypothesisPackingRule,
    apply_p17_trace,
    build_hypothesis_packing_root,
    enumerate_p17_sites,
)
from leanfaith.transforms.positives.v2_e0 import enumerate_p12_sites
from leanfaith.transforms.v2_e2_materializer import build_v2_e2_result
from leanfaith.transforms.v2_e2_p15_runtime import V2E2P15Runtime
from leanfaith.transforms.v2_e2_p16_runtime import V2E2P16Runtime
from leanfaith.transforms.v2_e2_p17_runtime import build_v2_e2_p17_runtime
from leanfaith.transforms.v2_e2_runtime import build_v2_e2_runtime
from leanfaith.transforms.v2_e2_scale_run import _seed, run_v2_e2_scale
from tests.unit.record_factories import representation_record, theorem_record

_PACK_SOURCE = "theorem p17 (P Q R : Prop) (hP : P) (hQ : Q) : R := by sorry"
_PACK_CANDIDATE = "theorem p17 (P Q R : Prop) (h_p17 : P ∧ Q) : R := by sorry"
_UNPACK_SOURCE = "theorem p17 (P Q R : Prop) (h : P ∧ Q) : R := by sorry"
_UNPACK_CANDIDATE = "theorem p17 (P Q R : Prop) (h_p17_left : P) (h_p17_right : Q) : R := by sorry"


def _prop() -> dict[str, object]:
    return {"k": "sort", "u": "0"}


def _nat() -> dict[str, object]:
    return {"k": "const", "n": "Nat", "us": "[]"}


def _app(function: dict[str, object], argument: dict[str, object]) -> dict[str, object]:
    return {"k": "app", "fn": function, "arg": argument}


def _and(left: dict[str, object], right: dict[str, object]) -> dict[str, object]:
    return _app(
        _app({"k": "const", "n": "And", "us": "[]"}, left),
        right,
    )


def _forall(
    domain: dict[str, object],
    body: dict[str, object],
    *,
    binder_info: str = "default",
) -> dict[str, object]:
    return {"k": "forall", "bi": binder_info, "dom": domain, "body": body}


def _pack_root(
    *,
    dependent_body: bool = False,
    dependent_right: bool = False,
    duplicate: bool = False,
    target_info: str = "default",
) -> dict[str, object]:
    # Under hP,hQ the result R is bvar 2.  bvar 0/1 deliberately exercise
    # prohibited proof dependencies.
    body: dict[str, object] = {"k": "bvar", "i": 0 if dependent_body else 2}
    right_domain: dict[str, object] = {
        "k": "bvar",
        "i": 0 if dependent_right else (3 if duplicate else 2),
    }
    result = _forall(right_domain, body, binder_info=target_info)
    result = _forall({"k": "bvar", "i": 2}, result, binder_info=target_info)
    for _ in range(3):
        result = _forall(_prop(), result)
    return result


def _packed_root(
    *,
    dependent_body: bool = False,
    duplicate: bool = False,
) -> dict[str, object]:
    left: dict[str, object] = {"k": "bvar", "i": 2}
    right: dict[str, object] = {"k": "bvar", "i": 2 if duplicate else 1}
    body: dict[str, object] = {"k": "bvar", "i": 0 if dependent_body else 1}
    result = _forall(_and(left, right), body)
    for _ in range(3):
        result = _forall(_prop(), result)
    return result


def _p12_cooccurrence_root() -> dict[str, object]:
    arrow = _forall({"k": "bvar", "i": 3}, {"k": "bvar", "i": 3})
    result = _forall({"k": "bvar", "i": 3}, arrow)
    result = _forall({"k": "bvar", "i": 3}, result)
    for _ in range(4):
        result = _forall(_prop(), result)
    return result


def _data_root() -> dict[str, object]:
    # A : Type, x : A, y : A |- A
    body: dict[str, object] = {"k": "bvar", "i": 2}
    result = _forall({"k": "bvar", "i": 1}, body)
    result = _forall({"k": "bvar", "i": 0}, result)
    result = _forall(_nat(), result)
    return result


def _records(
    source: str,
    key: str,
    root: dict[str, object],
) -> tuple[TheoremRecord, RepresentationRecord]:
    theorem_id = make_id("thm", {"p17": key})
    ancestry_id = make_id("anc", {"p17": key})
    theorem = theorem_record(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        declaration_name="p17",
        declaration_full_name="p17",
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
        representation_id=make_id("repr", {"p17": key}),
        theorem_id=theorem_id,
        raw_proof_stripped=source,
        headless=source,
        signature_pp=source,
        signature_explicit=source,
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
    candidate_root: dict[str, object],
) -> tuple[TheoremRecord, RepresentationRecord]:
    candidate = build_derived_theorem_record(
        draft=draft,
        sources=(source,),
        primary_source_id=source.theorem_id,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        inline_elaboration_source="import LeanFaithFixtures\n" + draft.candidate_code,
    )
    candidate_representation = source_representation.model_copy(
        update={
            "representation_id": make_id("repr", {"p17_candidate": candidate.theorem_id}),
            "theorem_id": candidate.theorem_id,
            "raw_proof_stripped": draft.candidate_code,
            "headless": draft.candidate_code,
            "signature_pp": draft.candidate_code,
            "signature_explicit": draft.candidate_code,
            "semantic_atoms": semantic_atoms(candidate_root),
            "operator_tree": operator_tree(candidate_root),
            "alpha_identity_fingerprint": alpha_identity_fingerprint(candidate_root),
        }
    )
    return candidate, candidate_representation


def test_p17_pack_and_unpack_are_distinct_exact_inverse_operations() -> None:
    (pack,) = enumerate_p17_sites(_PACK_SOURCE, operator_tree(_pack_root()))
    assert pack.operation == "pack_two"
    assert pack.candidate_text == "(h_p17 : P ∧ Q)"
    assert pack.left_proposition_text == "P"
    assert pack.right_proposition_text == "Q"
    assert pack.selected_surface_indices == (3, 4)
    assert pack.selected_outer_indices == (3, 4)
    assert pack.proposition_outer_indices == (0, 1)
    for certificate_hash in (
        pack.common_residual_hash,
        pack.dependency_proof_hash,
        pack.ordered_role_atom_hash,
        pack.expected_candidate_root_hash,
    ):
        assert len(certificate_hash) == 64
        int(certificate_hash, 16)
    assert (
        build_hypothesis_packing_root(
            _pack_root(),
            5,
            "pack_two",
            surface_names=("P", "Q", "R", "hP", "hQ"),
        )
        == _packed_root()
    )

    (unpack,) = enumerate_p17_sites(_UNPACK_SOURCE, operator_tree(_packed_root()))
    assert unpack.operation == "unpack_pair"
    assert unpack.candidate_text == "(h_p17_left : P) (h_p17_right : Q)"
    assert unpack.selected_surface_indices == (3,)
    assert unpack.selected_outer_indices == (3,)
    assert unpack.proposition_outer_indices == (0, 1)
    assert unpack.common_residual_hash == pack.common_residual_hash
    assert unpack.ordered_role_atom_hash == pack.ordered_role_atom_hash
    assert (
        build_hypothesis_packing_root(
            _packed_root(),
            4,
            "unpack_pair",
            surface_names=("P", "Q", "R", "h"),
        )
        == _pack_root()
    )

    pack_candidate = _PACK_SOURCE[: pack.start] + pack.candidate_text + _PACK_SOURCE[pack.end :]
    (inverse_unpack,) = enumerate_p17_sites(pack_candidate, operator_tree(_packed_root()))
    assert inverse_unpack.operation == "unpack_pair"
    unpack_candidate = (
        _UNPACK_SOURCE[: unpack.start] + unpack.candidate_text + _UNPACK_SOURCE[unpack.end :]
    )
    (inverse_pack,) = enumerate_p17_sites(unpack_candidate, operator_tree(_pack_root()))
    assert inverse_pack.operation == "pack_two"


@pytest.mark.parametrize(
    ("source", "root"),
    [
        (
            "theorem p17 (P Q R : Prop) (hP : P) (hQ : Q) : hP = hP := by sorry",
            _pack_root(dependent_body=True),
        ),
        (
            "theorem p17 (P Q R : Prop) (hP : P) (hQ : hP = hP) : R := by sorry",
            _pack_root(dependent_right=True),
        ),
        (
            "theorem p17 (P R : Prop) (hP : P) (hP2 : P) : R := by sorry",
            _pack_root(duplicate=True),
        ),
        (
            "theorem p17 (P Q R : Prop) (hP hQ : P) : R := by sorry",
            _pack_root(duplicate=True),
        ),
        (
            "theorem p17 (P Q R : Prop) (hP : P) [hQ : Q] : R := by sorry",
            _pack_root(target_info="instImplicit"),
        ),
        (
            "theorem p17 (P Q R : Prop) (hP : P) /- gap -/ (hQ : Q) : R := by sorry",
            _pack_root(),
        ),
        (
            "theorem p17 (P Q R : Prop) (hP : P) (hQ : Q) (n : Nat) : R := by sorry",
            _pack_root(),
        ),
        (
            "theorem p17 (P Q R : Prop) (h_p17 : R) (hP : P) (hQ : Q) : R := by sorry",
            _pack_root(),
        ),
    ],
)
def test_p17_rejects_dependency_duplicates_grouping_instances_nonfinal_and_collisions(
    source: str,
    root: dict[str, object],
) -> None:
    assert enumerate_p17_sites(source, operator_tree(root)) == ()


def test_p17_rejects_data_binders_and_coexists_disjointly_with_p12() -> None:
    data = "theorem p17 (A : Type) (x : A) (y : A) : A := by sorry"
    assert enumerate_p17_sites(data, operator_tree(_data_root())) == ()

    p12 = "theorem p17 (P Q R S : Prop) (hP : P) (hQ : Q) : R → S := by sorry"
    (p12_site,) = enumerate_p12_sites(p12)
    (p17_site,) = enumerate_p17_sites(p12, operator_tree(_p12_cooccurrence_root()))
    assert p17_site.p12_site_count == 1
    assert p17_site.end <= p12_site.start
    assert enumerate_p12_sites(_PACK_SOURCE) == ()
    assert enumerate_p12_sites(_PACK_CANDIDATE) == ()


def test_p17_generation_round_trips_and_remains_unresolved() -> None:
    theorem, representation = _records(_PACK_SOURCE, "generate", _pack_root())
    rule = P17HypothesisPackingRule(
        generation_config_hash="a" * 64,
        candidate_pool="fixture",
    )
    (draft,) = rule.generate(theorem, representation, seed=17)
    assert draft.candidate_code == _PACK_CANDIDATE
    assert draft.intended_relation == IntendedRelation.EQUIVALENT
    assert draft.intended_error_types == ()
    assert draft.inverse_trace is not None
    assert apply_p17_trace(draft.candidate_code, draft.inverse_trace) == _PACK_SOURCE
    assert draft.metadata["resolved_semantic_label"] is False
    assert draft.metadata["training_eligible"] is False


def test_p17_runtime_binds_its_separate_frozen_profile() -> None:
    theorem, representation = _records(_PACK_SOURCE, "runtime", _pack_root())
    runtime = build_v2_e2_p17_runtime()
    assert runtime.rule_ids == ("p17_hypothesis_packing",)
    execution = runtime.execute("p17_hypothesis_packing", theorem, representation, seed=17)
    assert execution.attempt.terminal_outcome == "generated"
    assert execution.attempt.registry_hash == runtime.portfolio_hash
    assert execution.drafts[0].candidate_code == _PACK_CANDIDATE


def test_profile_dispatch_preserves_p15_p16_identity_and_seed_bytes() -> None:
    root = Path(__file__).parents[2]
    p15 = build_v2_e2_runtime(
        root,
        path=root / "configs/transformations/v2_e2_p15_experimental.yaml",
    )
    p16 = build_v2_e2_runtime(
        root,
        path=root / "configs/transformations/v2_e2_p16_experimental.yaml",
    )
    p17 = build_v2_e2_runtime(
        root,
        path=root / "configs/transformations/v2_e2_p17_experimental.yaml",
    )
    assert isinstance(p15, V2E2P15Runtime)
    assert isinstance(p16, V2E2P16Runtime)
    assert p17.rule_ids == ("p17_hypothesis_packing",)
    theorem_id = "thm:" + "a" * 64
    assert _seed(17, theorem_id) == 7631974939362288428
    assert _seed(17, theorem_id, "p15_root_iff_reversal") == 7631974939362288428
    assert _seed(17, theorem_id, "p16_conjunction_reassociation") != 7631974939362288428
    assert _seed(17, theorem_id, "p17_hypothesis_packing") != _seed(
        17,
        theorem_id,
        "p16_conjunction_reassociation",
    )


@pytest.mark.parametrize(
    ("source", "source_root", "candidate_root", "expected_candidate"),
    [
        (_PACK_SOURCE, _pack_root(), _packed_root(), _PACK_CANDIDATE),
        (_UNPACK_SOURCE, _packed_root(), _pack_root(), _UNPACK_CANDIDATE),
    ],
)
def test_p17_clean_audit_is_e2_provisional_with_hard_zero_credit(
    source: str,
    source_root: dict[str, object],
    candidate_root: dict[str, object],
    expected_candidate: str,
) -> None:
    theorem, representation = _records(source, expected_candidate, source_root)
    rule = P17HypothesisPackingRule(
        generation_config_hash="b" * 64,
        candidate_pool="fixture",
    )
    (draft,) = rule.generate(theorem, representation, seed=17)
    assert draft.candidate_code == expected_candidate
    candidate, candidate_representation = _candidate_records(
        theorem,
        representation,
        draft,
        candidate_root=candidate_root,
    )
    audit = rule.audit(theorem, representation, candidate, candidate_representation, draft)
    assert audit.violation_codes == ()
    assert audit.structural_diff_ok is True
    assert audit.atom_mapping_ok is True
    assert audit.inverse_or_roundtrip_ok is True
    assert audit.recommended_quality_tier == QualityTier.PROVISIONAL
    assert audit.metadata["evidence_class"] == "E2"
    assert audit.metadata["resolved_semantic_label"] is False
    assert audit.metadata["training_eligible"] is False


def test_p17_tampered_order_is_quarantined() -> None:
    theorem, representation = _records(_PACK_SOURCE, "tamper", _pack_root())
    rule = P17HypothesisPackingRule(
        generation_config_hash="c" * 64,
        candidate_pool="fixture",
    )
    (draft,) = rule.generate(theorem, representation, seed=17)
    swapped = _packed_root()
    packed = cast(
        dict[str, Any], cast(dict[str, Any], cast(dict[str, Any], swapped["body"])["body"])["body"]
    )
    packed["dom"] = _and({"k": "bvar", "i": 1}, {"k": "bvar", "i": 2})
    candidate, candidate_representation = _candidate_records(
        theorem,
        representation,
        draft,
        candidate_root=swapped,
    )
    audit = rule.audit(theorem, representation, candidate, candidate_representation, draft)
    assert "candidate_not_exact_hypothesis_packing" in audit.violation_codes
    assert audit.recommended_quality_tier == QualityTier.UNKNOWN


def test_p17_rejects_malformed_trace() -> None:
    theorem, representation = _records(_PACK_SOURCE, "trace", _pack_root())
    rule = P17HypothesisPackingRule(
        generation_config_hash="d" * 64,
        candidate_pool="fixture",
    )
    (draft,) = rule.generate(theorem, representation, seed=17)
    corrupted = (dict(draft.transformation_trace[0], expected_text="drift"),)
    with pytest.raises(P17HypothesisPackingError, match="trace_expected_text_mismatch"):
        apply_p17_trace(_PACK_SOURCE, corrupted)


def test_p17_scale_runner_resumes_immutable_profile_bound_batches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import json
    from collections.abc import Sequence

    import leanfaith.transforms.v2_e2_scale_run as scale_run_module
    from leanfaith.transforms.v2_e2_scale import V2E2MaterializationInput

    none_root = _forall(_prop(), {"k": "bvar", "i": 0})
    source = "theorem p17_none (P : Prop) : P := by sorry"
    records = tuple(_records(source, f"resume-{index}", none_root) for index in range(2))
    theorem_path = tmp_path / "theorems.jsonl"
    representation_path = tmp_path / "representations.jsonl"
    theorem_path.write_text(
        "".join(
            json.dumps(theorem.model_dump(mode="json"), sort_keys=True) + "\n"
            for theorem, _representation in records
        ),
        encoding="utf-8",
    )
    representation_path.write_text(
        "".join(
            json.dumps(representation.model_dump(mode="json"), sort_keys=True) + "\n"
            for _theorem, representation in records
        ),
        encoding="utf-8",
    )
    runtime = build_v2_e2_p17_runtime()
    calls = 0

    def fake_materialize(
        *,
        backend: object,
        runtime: object,
        inputs: Sequence[V2E2MaterializationInput],
        context_id: str,
        project_dir: Path,
        import_header: str,
    ) -> tuple[object, ...]:
        del backend, context_id, project_dir, import_header
        nonlocal calls
        calls += 1
        typed_runtime = cast(Any, runtime)
        results = []
        for item in inputs:
            execution = typed_runtime.execute(
                item.rule_id,
                item.theorem,
                item.representation,
                item.seed,
            )
            assert execution.attempt.terminal_outcome == "not_applicable"
            results.append(
                build_v2_e2_result(
                    schema_version=1,
                    profile_id=typed_runtime.loaded.config.profile_id,
                    profile_config_hash=typed_runtime.generation_config_hash,
                    rule_id=item.rule_id,
                    evidence_class="E2",
                    terminal_status="not_applicable",
                    attempt=execution.attempt,
                    resolved_label_count=0,
                    promoted_item_count=0,
                    training_eligible=False,
                )
            )
        return tuple(results)

    monkeypatch.setattr(scale_run_module, "materialize_v2_e2_batch", fake_materialize)
    output = tmp_path / "scale"
    first = run_v2_e2_scale(
        backend=cast(Any, object()),
        runtime=runtime,
        theorem_path=theorem_path,
        representation_path=representation_path,
        project_dir=tmp_path,
        import_header="import LeanFaithFixtures",
        output_dir=output,
        batch_size=1,
        base_seed=17,
    )
    first_manifest = first.manifest_path.read_bytes()
    first_results = first.results_path.read_bytes()
    assert calls == 2
    second = run_v2_e2_scale(
        backend=cast(Any, object()),
        runtime=runtime,
        theorem_path=theorem_path,
        representation_path=representation_path,
        project_dir=tmp_path,
        import_header="import LeanFaithFixtures",
        output_dir=output,
        batch_size=1,
        base_seed=17,
    )
    assert calls == 2
    assert second.manifest_path.read_bytes() == first_manifest
    assert second.results_path.read_bytes() == first_results
    assert second.result_count == 2

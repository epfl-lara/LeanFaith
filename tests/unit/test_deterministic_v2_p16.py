"""Focused LF-033 tests for the provisional P16 E2 family."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanResult, LeanStatus
from leanfaith.representations import alpha_identity_fingerprint
from leanfaith.representations.atoms import operator_tree, semantic_atoms
from leanfaith.schemas import CANONICAL_VIEW_NAMES, RepresentationRecord, TheoremRecord
from leanfaith.schemas.enums import IntendedRelation, QualityTier, ValidationStatus, ViewStatus
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.variant import VariantDraft
from leanfaith.transforms.materialize import build_derived_theorem_record
from leanfaith.transforms.negatives.n15_conjunct_omission import enumerate_n15_sites
from leanfaith.transforms.positives.p16_conjunction_reassociation import (
    P16ConjunctionReassociationError,
    P16ConjunctionReassociationRule,
    apply_p16_trace,
    build_conjunction_reassociation_root,
    certify_conjunction_reassociation,
    enumerate_p16_sites,
)
from leanfaith.transforms.v2_e2_p15_runtime import V2E2P15Runtime
from leanfaith.transforms.v2_e2_p16_runtime import build_v2_e2_p16_runtime
from leanfaith.transforms.v2_e2_runtime import build_v2_e2_runtime
from leanfaith.transforms.v2_e2_scale import (
    V2E2MaterializationInput,
    materialize_v2_e2_batch,
)
from leanfaith.transforms.v2_e2_scale_run import _seed
from tests.unit.record_factories import representation_record, theorem_record

_SOURCE = "theorem p16 (P Q R : Prop) : (P ∧ Q) ∧ R := by sorry"
_CANDIDATE = "theorem p16 (P Q R : Prop) : (P) ∧ ((Q) ∧ (R)) := by sorry"


def _prop() -> dict[str, object]:
    return {"k": "sort", "u": "0"}


def _app(function: dict[str, object], argument: dict[str, object]) -> dict[str, object]:
    return {"k": "app", "fn": function, "arg": argument}


def _and(left: dict[str, object], right: dict[str, object]) -> dict[str, object]:
    node: dict[str, object] = {"k": "const", "n": "And", "us": "[]"}
    return _app(_app(node, left), right)


def _body(*, association: str = "left", duplicate: bool = False) -> dict[str, object]:
    p = {"k": "bvar", "i": 2}
    q = {"k": "bvar", "i": 1}
    r = q if duplicate else {"k": "bvar", "i": 0}
    if association == "left":
        return _and(_and(p, q), r)
    return _and(p, _and(q, r))


def _root(*, association: str = "left", duplicate: bool = False) -> dict[str, object]:
    body = _body(association=association, duplicate=duplicate)
    for _ in range(3):
        body = {
            "k": "forall",
            "bi": "default",
            "dom": _prop(),
            "body": body,
        }
    return body


def _binary_root() -> dict[str, object]:
    body = _and({"k": "bvar", "i": 1}, {"k": "bvar", "i": 0})
    for _ in range(2):
        body = {"k": "forall", "bi": "default", "dom": _prop(), "body": body}
    return body


def _records(
    source: str, key: str, root: dict[str, object]
) -> tuple[TheoremRecord, RepresentationRecord]:
    theorem_id = make_id("thm", {"p16": key})
    ancestry_id = make_id("anc", {"p16": key})
    theorem = theorem_record(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        declaration_name="p16",
        declaration_full_name="p16",
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
        representation_id=make_id("repr", {"p16": key}),
        theorem_id=theorem_id,
        raw_proof_stripped=source,
        headless="(P Q R : Prop) : (P ∧ Q) ∧ R",
        signature_pp="(P Q R : Prop) : (P ∧ Q) ∧ R",
        signature_explicit="∀ (P Q R : Prop), And (And P Q) R",
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
    candidate = build_derived_theorem_record(
        draft=draft,
        sources=(source,),
        primary_source_id=source.theorem_id,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        inline_elaboration_source="import LeanFaithFixtures\n" + draft.candidate_code,
    )
    candidate_root = _root(association="right")
    candidate_representation = source_representation.model_copy(
        update={
            "representation_id": make_id("repr", {"p16_candidate": candidate.theorem_id}),
            "theorem_id": candidate.theorem_id,
            "raw_proof_stripped": draft.candidate_code,
            "headless": "(P Q R : Prop) : P ∧ (Q ∧ R)",
            "signature_pp": "(P Q R : Prop) : P ∧ (Q ∧ R)",
            "signature_explicit": "∀ (P Q R : Prop), And P (And Q R)",
            "semantic_atoms": semantic_atoms(candidate_root),
            "operator_tree": operator_tree(candidate_root),
            "alpha_identity_fingerprint": alpha_identity_fingerprint(candidate_root),
        }
    )
    return candidate, candidate_representation


def test_p16_exact_three_leaf_root_reassociation_preserves_order() -> None:
    (site,) = enumerate_p16_sites(_SOURCE, operator_tree(_root()))
    assert site.source_association == "left"
    assert site.atom_texts == ("P", "Q", "R")
    assert site.candidate_text == "(P) ∧ ((Q) ∧ (R))"
    assert build_conjunction_reassociation_root(_root(), 3) == _root(association="right")


def test_p16_supports_both_explicit_association_directions() -> None:
    source = "theorem p16 (P Q R : Prop) : P ∧ (Q ∧ R) := by sorry"
    (site,) = enumerate_p16_sites(source, operator_tree(_root(association="right")))
    assert site.source_association == "right"
    assert site.atom_texts == ("P", "Q", "R")
    assert site.candidate_text == "((P) ∧ (Q)) ∧ (R)"


def test_p16_and_n15_scopes_are_mutually_exclusive() -> None:
    assert enumerate_n15_sites(_SOURCE, operator_tree(_root())) == ()
    binary = "theorem n15 (P Q : Prop) : P ∧ Q := by sorry"
    assert enumerate_p16_sites(binary, operator_tree(_binary_root())) == ()
    assert len(enumerate_n15_sites(binary, operator_tree(_binary_root()))) == 2


@pytest.mark.parametrize(
    ("source", "root"),
    [
        ("theorem p16 (P Q : Prop) : (P ∧ Q) ∧ P := by sorry", _root(duplicate=True)),
        ("theorem p16 (P Q R : Prop) : P ∧ Q ∧ R := by sorry", _root(association="right")),
        ("theorem p16 (P Q R : Prop) : (P ∧ Q) ∧ (R ∧ P) := by sorry", _root()),
        ("theorem p16 (P Q R : Prop) : (P /- c -/ ∧ Q) ∧ R := by sorry", _root()),
        ("theorem p16 (P Q R : Prop) : (if P then Q else R) ∧ Q ∧ R := by sorry", _root()),
    ],
)
def test_p16_rejects_duplicates_implicit_or_deeper_chains_and_unsafe_surface(
    source: str, root: dict[str, object]
) -> None:
    assert enumerate_p16_sites(source, operator_tree(root)) == ()


def test_p16_generation_round_trips_and_remains_unresolved() -> None:
    theorem, representation = _records(_SOURCE, "generate", _root())
    rule = P16ConjunctionReassociationRule(
        generation_config_hash="d" * 64, candidate_pool="fixture"
    )
    (draft,) = rule.generate(theorem, representation, seed=16)
    assert draft.candidate_code == _CANDIDATE
    assert draft.intended_relation == IntendedRelation.EQUIVALENT
    assert draft.intended_error_types == ()
    assert draft.inverse_trace is not None
    assert apply_p16_trace(draft.candidate_code, draft.inverse_trace) == _SOURCE
    assert draft.metadata["resolved_semantic_label"] is False
    assert draft.metadata["training_eligible"] is False


def test_p16_runtime_binds_its_separate_frozen_profile() -> None:
    theorem, representation = _records(_SOURCE, "runtime", _root())
    runtime = build_v2_e2_p16_runtime()
    assert runtime.rule_ids == ("p16_conjunction_reassociation",)
    execution = runtime.execute("p16_conjunction_reassociation", theorem, representation, seed=16)
    assert execution.attempt.terminal_outcome == "generated"
    assert execution.attempt.family_id == "p16_conjunction_reassociation"
    assert execution.attempt.registry_hash == runtime.portfolio_hash
    assert execution.drafts[0].candidate_code == _CANDIDATE


def test_profile_dispatch_preserves_p15_identity_and_seed_bytes() -> None:
    root = Path(__file__).parents[2]
    p15 = build_v2_e2_runtime(
        root, path=root / "configs/transformations/v2_e2_p15_experimental.yaml"
    )
    p16 = build_v2_e2_runtime(
        root, path=root / "configs/transformations/v2_e2_p16_experimental.yaml"
    )
    assert isinstance(p15, V2E2P15Runtime)
    assert p15.rule_ids == ("p15_root_iff_reversal",)
    assert p16.rule_ids == ("p16_conjunction_reassociation",)
    theorem_id = "thm:" + "a" * 64
    assert _seed(17, theorem_id) == 7631974939362288428
    assert _seed(17, theorem_id, "p15_root_iff_reversal") == 7631974939362288428
    assert _seed(17, theorem_id, "p16_conjunction_reassociation") != 7631974939362288428


def test_p16_certificate_accepts_only_exact_reassociation() -> None:
    certificate = certify_conjunction_reassociation(_root(), _root(association="right"), 3)
    assert certificate.source_association == "left"
    assert len(certificate.atom_hashes) == 3
    with pytest.raises(P16ConjunctionReassociationError, match="candidate_not_exact"):
        certify_conjunction_reassociation(_root(), _root(), 3)


def test_p16_clean_audit_is_e2_provisional_with_hard_zero_credit() -> None:
    theorem, representation = _records(_SOURCE, "audit", _root())
    rule = P16ConjunctionReassociationRule(
        generation_config_hash="e" * 64, candidate_pool="fixture"
    )
    (draft,) = rule.generate(theorem, representation, seed=16)
    candidate, candidate_representation = _candidate_records(theorem, representation, draft)
    audit = rule.audit(theorem, representation, candidate, candidate_representation, draft)
    assert audit.violation_codes == ()
    assert audit.structural_diff_ok is True
    assert audit.inverse_or_roundtrip_ok is True
    assert audit.recommended_quality_tier == QualityTier.PROVISIONAL
    assert audit.metadata["evidence_class"] == "E2"
    assert audit.metadata["resolved_semantic_label"] is False
    assert audit.metadata["training_eligible"] is False


def test_p16_tampered_atom_order_is_quarantined() -> None:
    theorem, representation = _records(_SOURCE, "tamper", _root())
    rule = P16ConjunctionReassociationRule(
        generation_config_hash="f" * 64, candidate_pool="fixture"
    )
    (draft,) = rule.generate(theorem, representation, seed=16)
    candidate, candidate_representation = _candidate_records(theorem, representation, draft)
    audit = rule.audit(
        theorem,
        representation,
        candidate,
        candidate_representation.model_copy(
            update={
                "operator_tree": operator_tree(_root()),
                "alpha_identity_fingerprint": alpha_identity_fingerprint(_root()),
            }
        ),
        draft,
    )
    assert "candidate_not_exact_root_conjunction_reassociation" in audit.violation_codes
    assert audit.recommended_quality_tier == QualityTier.UNKNOWN


class _StatusBackend:
    def __init__(self, statuses: Sequence[LeanStatus]) -> None:
        self.statuses = tuple(statuses)

    def run(self, request: LeanRequest) -> LeanResult:
        raise AssertionError(f"unexpected sequential request: {request.request_id}")

    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
        return [
            LeanResult(
                request_id=request.request_id,
                request_hash=f"{index + 1:064x}",
                context_id=request.context_id,
                context_fingerprint=request.context_id.removeprefix("ctx:"),
                status=self.statuses[index],
                infrastructure_error=(
                    "failed to create thread" if self.statuses[index] == LeanStatus.CRASH else None
                ),
            )
            for index, request in enumerate(requests)
        ]


def _materialization_input(key: str) -> V2E2MaterializationInput:
    theorem, representation = _records(_SOURCE, key, _root())
    return V2E2MaterializationInput(
        theorem=theorem,
        representation=representation,
        rule_id="p16_conjunction_reassociation",
        seed=16,
    )


def test_e2_scale_separates_semantic_invalidity_from_every_infrastructure_failure(
    tmp_path: Path,
) -> None:
    statuses = (
        LeanStatus.INVALID,
        LeanStatus.CRASH,
        LeanStatus.SETUP_ERROR,
        LeanStatus.TIMEOUT,
        LeanStatus.INTERNAL_ERROR,
        LeanStatus.UNSUPPORTED,
    )
    inputs = tuple(_materialization_input(status.value) for status in statuses)
    results = materialize_v2_e2_batch(
        backend=cast(
            LeanInteractBackend,
            _StatusBackend(statuses),
        ),
        runtime=build_v2_e2_p16_runtime(),
        inputs=inputs,
        context_id=inputs[0].theorem.context_id,
        project_dir=tmp_path,
        import_header="import LeanFaithFixtures",
    )
    assert results[0].terminal_status == "candidate_invalid"
    assert all(result.terminal_status == "candidate_infrastructure_error" for result in results[1:])
    assert results[1].failure_codes == ("lean_crash",)
    assert all(result.resolved_label_count == 0 for result in results)
    assert all(result.promoted_item_count == 0 for result in results)
    assert all(result.training_eligible is False for result in results)


def test_e2_scale_stops_missing_candidate_views_before_semantic_audit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import leanfaith.transforms.v2_e2_scale as scale_module

    item = _materialization_input("representation_failure")

    def failed_representation(
        backend: object, inputs: list[object], **kwargs: object
    ) -> list[RepresentationRecord]:
        del backend, kwargs
        candidate_input = inputs[0]
        theorem_id = cast(Any, candidate_input).theorem_id
        statuses = {
            **item.representation.view_status,
            "operator_tree": ViewStatus.FAILED,
        }
        return [
            item.representation.model_copy(
                update={
                    "representation_id": make_id("repr", {"p16_failed_candidate": theorem_id}),
                    "theorem_id": theorem_id,
                    "operator_tree": None,
                    "view_status": statuses,
                }
            )
        ]

    monkeypatch.setattr(scale_module, "build_representations", failed_representation)
    (result,) = materialize_v2_e2_batch(
        backend=cast(
            LeanInteractBackend,
            _StatusBackend((LeanStatus.VALID_WITH_SORRY,)),
        ),
        runtime=build_v2_e2_p16_runtime(),
        inputs=(item,),
        context_id=item.theorem.context_id,
        project_dir=tmp_path,
        import_header="import LeanFaithFixtures",
    )
    assert result.terminal_status == "candidate_representation_failed"
    assert result.audit is None
    assert result.variant is None
    assert result.failure_codes == ("representation_operator_tree_failed",)
    assert result.training_eligible is False

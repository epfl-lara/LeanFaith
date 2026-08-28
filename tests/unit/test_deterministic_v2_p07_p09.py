"""Focused tests for conservative deterministic-v2 P07/P09 rule code."""

from __future__ import annotations

import hashlib

import pytest

from leanfaith.representations import alpha_identity_fingerprint
from leanfaith.schemas import CANONICAL_VIEW_NAMES, ViewStatus, make_id
from leanfaith.schemas.enums import IntendedRelation, QualityTier, ValidationStatus
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms.materialize import build_derived_theorem_record
from leanfaith.transforms.positives.v2_e0 import apply_presentation_trace
from leanfaith.transforms.positives.v2_e0_p07_p09 import (
    P07CoercionSurfaceRule,
    P09ProjectionSurfaceRule,
    enumerate_p07_sites,
    enumerate_p09_sites,
)
from tests.unit.record_factories import representation_record, theorem_record

_P07_SOURCE = "theorem coerced (n : Nat) : (↑n : Int) = 0 := by sorry"
_P09_FIRST_SOURCE = "theorem first (p : Nat \N{MULTIPLICATION SIGN} Nat) : p.1 = 0 := by sorry"
_P09_SECOND_SOURCE = "theorem second (p : Prod Nat Nat) : p.snd = 0 := by sorry"


def _statuses() -> dict[str, ViewStatus]:
    return {
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


def _source_records(
    *,
    source: str,
    key: str,
    semantic_atoms: tuple[str, ...],
    root: dict[str, object],
) -> tuple[TheoremRecord, RepresentationRecord]:
    theorem_id = make_id("thm", {"v2_p07_p09": key})
    ancestry_id = make_id("anc", {"v2_p07_p09": key})
    theorem = theorem_record(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(ancestry_id,),
        declaration_name=key,
        declaration_full_name=key,
        proof_stripped_declaration=source,
        inline_elaboration_source="import LeanFaithFixtures\n" + source,
        statement_content_hash=hashlib.sha256(source.encode("utf-8")).hexdigest(),
    )
    tree = {
        "atom_version": "atoms_v1",
        "node_count": 1,
        "depth": 1,
        "root": root,
    }
    representation = representation_record(
        representation_id=make_id("repr", {"v2_p07_p09": key}),
        theorem_id=theorem_id,
        raw_proof_stripped=source,
        headless=source.split(":", 1)[-1].split(":=", 1)[0],
        signature_pp="fixture type",
        signature_explicit="fixture explicit type",
        semantic_atoms=semantic_atoms,
        operator_tree=tree,
        alpha_identity_fingerprint=alpha_identity_fingerprint(tree),
        view_status=_statuses(),
    )
    return theorem, representation


def _p07_records(
    source: str = _P07_SOURCE,
    *,
    atoms: tuple[str, ...] = ("const:Eq", "const:Coe.coe"),
    constants: tuple[str, ...] = ("Coe.coe",),
    key: str = "p07",
) -> tuple[TheoremRecord, RepresentationRecord]:
    root: dict[str, object] = {
        "k": "app",
        "fn": {"k": "const", "n": constants[0]} if constants else {"k": "bvar", "i": 0},
        "arg": {"k": "bvar", "i": 0},
    }
    if len(constants) > 1:
        root = {
            "k": "app",
            "fn": {"k": "const", "n": constants[0]},
            "arg": {
                "k": "app",
                "fn": {"k": "const", "n": constants[1]},
                "arg": {"k": "bvar", "i": 0},
            },
        }
    return _source_records(
        source=source,
        key=key,
        semantic_atoms=atoms,
        root=root,
    )


def _p09_records(
    source: str = _P09_FIRST_SOURCE,
    *,
    field_index: int = 0,
    atoms: tuple[str, ...] | None = None,
    extra_projection: bool = False,
    key: str = "p09",
) -> tuple[TheoremRecord, RepresentationRecord]:
    projection: dict[str, object] = {
        "k": "proj",
        "s": "Prod",
        "i": field_index,
        "base": {"k": "bvar", "i": 0},
    }
    root: dict[str, object]
    if extra_projection:
        root = {
            "k": "app",
            "fn": projection,
            "arg": {
                "k": "proj",
                "s": "Prod",
                "i": field_index,
                "base": {"k": "bvar", "i": 0},
            },
        }
    else:
        root = projection
    return _source_records(
        source=source,
        key=key,
        semantic_atoms=atoms or ("const:Eq", f"proj:Prod:{field_index}"),
        root=root,
    )


def _p09_accessor_records() -> tuple[TheoremRecord, RepresentationRecord]:
    return _source_records(
        source=_P09_FIRST_SOURCE,
        key="p09-accessor",
        semantic_atoms=("const:Eq", "const:Prod.fst"),
        root={
            "k": "app",
            "fn": {"k": "const", "n": "Prod.fst"},
            "arg": {"k": "bvar", "i": 0},
        },
    )


def _candidate_representation(
    candidate: TheoremRecord,
    source: RepresentationRecord,
) -> RepresentationRecord:
    return source.model_copy(
        update={
            "representation_id": make_id("repr", {"candidate": candidate.theorem_id}),
            "theorem_id": candidate.theorem_id,
            "raw_proof_stripped": candidate.proof_stripped_declaration,
        }
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (_P07_SOURCE, "theorem coerced (n : Nat) : (n : Int) = 0 := by sorry"),
        ("theorem implicit (n : Nat) : (n : Int) = 0 := by sorry", None),
        ("theorem comment (n : Nat) : True /- (↑n : Int) -/ := by sorry", None),
    ],
)
def test_p07_source_enumerator_is_explicit_only_and_exact(
    source: str,
    expected: str | None,
) -> None:
    sites = enumerate_p07_sites(source)
    if expected is None:
        assert sites == ()
        return
    assert len(sites) == 1
    site = sites[0]
    assert site.operation == "hide_explicit_coercion"
    assert source[: site.start] + site.replacement_text + source[site.end :] == expected


def test_p07_generation_has_exact_inverse_and_no_semantic_credit() -> None:
    theorem, representation = _p07_records()
    rule = P07CoercionSurfaceRule(
        generation_config_hash="7" * 64,
        candidate_pool="deterministic_v2_e0_experimental",
    )
    applicability = rule.assess(theorem, representation)
    assert applicability.applicable
    drafts = rule.generate(theorem, representation, seed=17)
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.intended_relation == IntendedRelation.EQUIVALENT
    assert draft.candidate_code == "theorem coerced (n : Nat) : (n : Int) = 0 := by sorry"
    assert draft.inverse_trace is not None
    assert apply_presentation_trace(draft.candidate_code, draft.inverse_trace) == _P07_SOURCE
    assert draft.metadata == {"generation_intention_only": True}


@pytest.mark.parametrize(
    ("atoms", "constants", "reason"),
    [
        (("const:Eq",), (), "coercion_atom_not_unique"),
        (
            ("const:Eq", "const:Coe.coe", "const:CoeT.coe"),
            ("Coe.coe", "CoeT.coe"),
            "elaborated_coercion_hop_not_unique",
        ),
        (
            ("const:Eq", "const:CoeFun.coe"),
            ("CoeFun.coe",),
            "coercion_kind_excluded",
        ),
    ],
)
def test_p07_fails_closed_without_one_ordinary_elaborated_hop(
    atoms: tuple[str, ...],
    constants: tuple[str, ...],
    reason: str,
) -> None:
    theorem, representation = _p07_records(
        atoms=atoms,
        constants=constants,
        key=reason,
    )
    applicability = P07CoercionSurfaceRule(
        generation_config_hash="7" * 64,
        candidate_pool="fixture",
    ).assess(theorem, representation)
    assert not applicability.applicable
    assert reason in applicability.reason_codes


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            _P09_FIRST_SOURCE,
            "theorem first (p : Nat \N{MULTIPLICATION SIGN} Nat) : p.fst = 0 := by sorry",
        ),
        (_P09_SECOND_SOURCE, "theorem second (p : Prod Nat Nat) : p.2 = 0 := by sorry"),
    ],
)
def test_p09_source_enumerator_switches_numeric_and_named_syntax(
    source: str,
    expected: str,
) -> None:
    sites = enumerate_p09_sites(source)
    assert len(sites) == 1
    site = sites[0]
    assert source[: site.start] + site.replacement_text + source[site.end :] == expected


@pytest.mark.parametrize(
    "source",
    [
        "theorem chain (p : Nat \N{MULTIPLICATION SIGN} Nat) : p.1.succ = 0 := by sorry",
        "theorem ambiguous (p : Nat) : p.1 = 0 := by sorry",
        "theorem update (p : Nat \N{MULTIPLICATION SIGN} Nat) : "
        "{ p with fst := 0 }.1 = 0 := by sorry",
    ],
)
def test_p09_source_enumerator_rejects_excluded_surface_shapes(source: str) -> None:
    assert enumerate_p09_sites(source) == ()


def test_p09_requires_one_matching_direct_projection_without_coercion() -> None:
    rule = P09ProjectionSurfaceRule(
        generation_config_hash="9" * 64,
        candidate_pool="fixture",
    )

    wrong_theorem, wrong_representation = _p09_records(field_index=1, key="wrong-index")
    wrong = rule.assess(wrong_theorem, wrong_representation)
    assert not wrong.applicable
    assert "direct_projection_node_mismatch" in wrong.reason_codes

    many_theorem, many_representation = _p09_records(
        extra_projection=True,
        key="many-projections",
    )
    many = rule.assess(many_theorem, many_representation)
    assert not many.applicable
    assert "direct_projection_evidence_not_unique" in many.reason_codes

    coe_theorem, coe_representation = _p09_records(
        atoms=("const:Eq", "proj:Prod:0", "const:Coe.coe"),
        key="coercion-field",
    )
    coe = rule.assess(coe_theorem, coe_representation)
    assert not coe.applicable
    assert "coercion_field_excluded" in coe.reason_codes

    accessor_theorem, accessor_representation = _p09_accessor_records()
    accessor = rule.assess(accessor_theorem, accessor_representation)
    assert accessor.applicable


@pytest.mark.parametrize(
    ("rule_class", "records"),
    [
        (P07CoercionSurfaceRule, _p07_records),
        (P09ProjectionSurfaceRule, _p09_records),
    ],
)
def test_clean_audit_is_only_provisional_and_detector_tamper_quarantines(
    rule_class: type[P07CoercionSurfaceRule] | type[P09ProjectionSurfaceRule],
    records: object,
) -> None:
    theorem, representation = records()  # type: ignore[operator]
    rule = rule_class(
        generation_config_hash="a" * 64,
        candidate_pool="deterministic_v2_e0_experimental",
    )
    draft = rule.generate(theorem, representation, seed=5)[0]
    candidate = build_derived_theorem_record(
        draft=draft,
        sources=(theorem,),
        primary_source_id=theorem.theorem_id,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        inline_elaboration_source="import LeanFaithFixtures\n" + draft.candidate_code,
    )
    candidate_representation = _candidate_representation(candidate, representation)
    audit = rule.audit(
        theorem,
        representation,
        candidate,
        candidate_representation,
        draft,
    )
    assert audit.violation_codes == ()
    assert audit.recommended_quality_tier == QualityTier.PROVISIONAL
    assert audit.metadata["resolved_semantic_label"] is False
    assert audit.metadata["training_eligible"] is False

    if rule_class is P07CoercionSurfaceRule:
        tampered_source = representation.model_copy(update={"semantic_atoms": ("const:Eq",)})
        tampered_candidate = candidate_representation.model_copy(
            update={"semantic_atoms": ("const:Eq",)}
        )
    else:
        tampered_source = representation.model_copy(update={"semantic_atoms": ("const:Eq",)})
        tampered_candidate = candidate_representation.model_copy(
            update={"semantic_atoms": ("const:Eq",)}
        )
    rejected = rule.audit(
        theorem,
        tampered_source,
        candidate,
        tampered_candidate,
        draft,
    )
    assert "source_detector_contract_mismatch" in rejected.violation_codes
    assert rejected.recommended_validation_status == ValidationStatus.QUARANTINED
    assert rejected.recommended_quality_tier == QualityTier.UNKNOWN

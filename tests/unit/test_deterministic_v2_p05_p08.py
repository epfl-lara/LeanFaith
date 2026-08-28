"""Focused fail-closed tests for experimental P05/P08 E0 rules."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from leanfaith.config.paths import find_repo_root
from leanfaith.representations import alpha_identity_fingerprint
from leanfaith.schemas import CANONICAL_VIEW_NAMES, ViewStatus, make_id
from leanfaith.schemas.enums import IntendedRelation, QualityTier, ValidationStatus
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms.materialize import build_derived_theorem_record
from leanfaith.transforms.positives.p05_p08_surface import (
    MAX_VARIANTS_PER_SOURCE_PER_FAMILY,
    P05_POSITIVE_SLOT_CAP,
    P08_POSITIVE_SLOT_CAP,
    P05ResolvedGlobalNamesRule,
    P08TypeAscriptionsRule,
    enumerate_p05_sites,
    enumerate_p08_sites,
)
from leanfaith.transforms.positives.v2_e0 import apply_presentation_trace
from tests.unit.record_factories import representation_record, theorem_record

_ROOT = find_repo_root(Path(__file__).parent)


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


def _records(
    source: str,
    *,
    key: str,
    semantic_atoms: tuple[str, ...],
    root: dict[str, object],
) -> tuple[TheoremRecord, RepresentationRecord]:
    theorem_id = make_id("thm", {"v2_p05_p08": key})
    ancestry_id = make_id("anc", {"v2_p05_p08": key})
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
        representation_id=make_id("repr", {"v2_p05_p08": key}),
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


def _p05_records(
    source: str = "theorem p (n : Nat) : Nat.succ n = n + 1 := by sorry",
    *,
    constants: tuple[str, ...] = ("Nat.succ",),
    atoms: tuple[str, ...] = ("const:Nat.succ",),
    key: str = "p05",
) -> tuple[TheoremRecord, RepresentationRecord]:
    nodes: list[dict[str, object]] = [{"k": "const", "n": name, "us": []} for name in constants]
    root: dict[str, object] = (
        nodes[0] if len(nodes) == 1 else {"k": "app", "fn": nodes[0], "arg": nodes[1]}
    )
    return _records(source, key=key, semantic_atoms=atoms, root=root)


def _p08_records(
    source: str = "theorem p (n : Nat) : n = 0 := by sorry",
    *,
    key: str = "p08",
    root: dict[str, object] | None = None,
    atoms: tuple[str, ...] = ("const:Eq", "const:Nat"),
) -> tuple[TheoremRecord, RepresentationRecord]:
    return _records(
        source,
        key=key,
        semantic_atoms=atoms,
        root=root
        or {
            "k": "forall",
            "bi": "default",
            "dom": {"k": "const", "n": "Nat", "us": []},
            "body": {"k": "bvar", "i": 0},
        },
    )


@pytest.mark.parametrize(
    ("source", "expected", "operation"),
    [
        (
            "theorem p (n : Nat) : Nat.succ n = n + 1 := by sorry",
            "theorem p (n : Nat) : _root_.Nat.succ n = n + 1 := by sorry",
            "insert_root_qualifier",
        ),
        (
            "theorem p (n : Nat) : _root_.Nat.succ n = n + 1 := by sorry",
            "theorem p (n : Nat) : Nat.succ n = n + 1 := by sorry",
            "remove_root_qualifier",
        ),
    ],
)
def test_p05_toggles_only_root_qualification_with_exact_inverse(
    source: str,
    expected: str,
    operation: str,
) -> None:
    theorem, representation = _p05_records(source, key=operation)
    rule = P05ResolvedGlobalNamesRule(
        generation_config_hash="5" * 64,
        candidate_pool="deterministic_v2_e0_experimental",
    )
    assert rule.assess(theorem, representation).applicable
    draft = rule.generate(theorem, representation, seed=5)[0]
    assert draft.candidate_code == expected
    assert draft.transformation_trace[0]["presentation_operation"] == operation
    assert draft.inverse_trace is not None
    assert apply_presentation_trace(expected, draft.inverse_trace) == source
    assert draft.intended_relation == IntendedRelation.EQUIVALENT
    assert draft.metadata == {"generation_intention_only": True}


@pytest.mark.parametrize(
    "source",
    [
        # Bare suffixes are never guessed or rewritten.
        "theorem p (n : Nat) : succ n = n := by sorry",
        # Public-looking but non-allowlisted declarations fail closed.
        "theorem p (n : Nat) : Nat.add n 0 = n := by sorry",
        # Private/macro/alias-looking names are outside the code-owned list.
        "theorem p (n : Nat) : _private.foo n = n := by sorry",
        # A local namespace-root or terminal suffix may shadow resolution.
        "theorem p (Nat : Nat → Nat) (n : Nat) : Nat.succ n = n := by sorry",
        "theorem p (succ : Nat → Nat) (n : Nat) : Nat.succ n = n := by sorry",
        # One family application means exactly one eligible source site.
        "theorem p (n : Nat) : Nat.succ n = Nat.succ n := by sorry",
    ],
)
def test_p05_source_scope_rejects_suffix_alias_shadow_and_multiple_cases(source: str) -> None:
    sites = enumerate_p05_sites(source)
    if source.count("Nat.succ") == 2:
        assert len(sites) == 2
    else:
        assert sites == ()


def test_p05_representation_requires_one_global_and_unambiguous_suffix() -> None:
    theorem, representation = _p05_records(
        constants=("Nat.succ", "Other.succ"),
        atoms=("const:Nat.succ", "const:Other.succ"),
        key="ambiguous",
    )
    result = P05ResolvedGlobalNamesRule(
        generation_config_hash="5" * 64,
        candidate_pool="fixture",
    ).assess(theorem, representation)
    assert not result.applicable
    assert "resolved_global_suffix_ambiguous" in result.reason_codes

    duplicate_theorem, duplicate_representation = _p05_records(
        constants=("Nat.succ", "Nat.succ"),
        atoms=("const:Nat.succ", "const:Nat.succ"),
        key="duplicate",
    )
    duplicate = P05ResolvedGlobalNamesRule(
        generation_config_hash="5" * 64,
        candidate_pool="fixture",
    ).assess(duplicate_theorem, duplicate_representation)
    assert not duplicate.applicable
    assert "resolved_global_node_not_unique" in duplicate.reason_codes


@pytest.mark.parametrize(
    ("source", "expected", "operation"),
    [
        (
            "theorem p (n : Nat) : n = 0 := by sorry",
            "theorem p (n : Nat) : (n : Nat) = 0 := by sorry",
            "insert_redundant_type_ascription",
        ),
        (
            "theorem p (n : Nat) : (n : Nat) = 0 := by sorry",
            "theorem p (n : Nat) : n = 0 := by sorry",
            "remove_redundant_type_ascription",
        ),
    ],
)
def test_p08_inserts_or_removes_one_source_printable_ascription(
    source: str,
    expected: str,
    operation: str,
) -> None:
    theorem, representation = _p08_records(source, key=operation)
    rule = P08TypeAscriptionsRule(
        generation_config_hash="8" * 64,
        candidate_pool="deterministic_v2_e0_experimental",
    )
    assert rule.assess(theorem, representation).applicable
    draft = rule.generate(theorem, representation, seed=8)[0]
    assert draft.candidate_code == expected
    assert draft.transformation_trace[0]["presentation_operation"] == operation
    assert draft.inverse_trace is not None
    assert apply_presentation_trace(expected, draft.inverse_trace) == source


@pytest.mark.parametrize(
    "source",
    [
        # Binder declarations are outside the conclusion and never edited.
        "theorem p (n : Nat) : True := by sorry",
        # The ascription type must exactly match the source binder type.
        "theorem p (n : Nat) : (n : Int) = 0 := by sorry",
        # Applied/non-atomic type syntax is outside this first executable cap.
        "theorem p (xs : List Nat) : xs = [] := by sorry",
        # Multiple term sites are rejected by the elaborated detector.
        "theorem p (n : Nat) : n = n := by sorry",
        # Projection receivers are intentionally not selected.
        "theorem p (n : Nat) : n.succ = 0 := by sorry",
    ],
)
def test_p08_fails_closed_outside_one_simple_term_site(source: str) -> None:
    sites = enumerate_p08_sites(source)
    if source.endswith(": n = n := by sorry"):
        assert len(sites) == 2
    else:
        assert sites == ()


def test_p08_detector_rejects_coercion_owned_and_metavariable_cases() -> None:
    coercion_theorem, coercion_representation = _p08_records(
        key="coercion",
        root={
            "k": "app",
            "fn": {"k": "const", "n": "Coe.coe", "us": []},
            "arg": {"k": "bvar", "i": 0},
        },
        atoms=("const:Eq", "const:Coe.coe"),
    )
    rule = P08TypeAscriptionsRule(
        generation_config_hash="8" * 64,
        candidate_pool="fixture",
    )
    coercion = rule.assess(coercion_theorem, coercion_representation)
    assert not coercion.applicable
    assert "coercion_owned_case_excluded" in coercion.reason_codes

    mvar_theorem, mvar_representation = _p08_records(
        key="mvar",
        root={"k": "mvar", "n": "?m.1"},
    )
    mvar = rule.assess(mvar_theorem, mvar_representation)
    assert not mvar.applicable
    assert "metavariable_type_excluded" in mvar.reason_codes


@pytest.mark.parametrize(
    ("rule_class", "records"),
    [
        (P05ResolvedGlobalNamesRule, _p05_records),
        (P08TypeAscriptionsRule, _p08_records),
    ],
)
def test_clean_exact_identity_audit_remains_provisional_and_nontraining(
    rule_class: type[P05ResolvedGlobalNamesRule] | type[P08TypeAscriptionsRule],
    records: object,
) -> None:
    theorem, representation = records()  # type: ignore[operator]
    rule = rule_class(
        generation_config_hash="a" * 64,
        candidate_pool="deterministic_v2_e0_experimental",
    )
    draft = rule.generate(theorem, representation, seed=0)[0]
    candidate = build_derived_theorem_record(
        draft=draft,
        sources=(theorem,),
        primary_source_id=theorem.theorem_id,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        inline_elaboration_source="import LeanFaithFixtures\n" + draft.candidate_code,
    )
    candidate_representation = representation.model_copy(
        update={
            "representation_id": make_id("repr", {"candidate": candidate.theorem_id}),
            "theorem_id": candidate.theorem_id,
            "raw_proof_stripped": candidate.proof_stripped_declaration,
        }
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
    assert audit.metadata["resolved_semantic_label"] is False
    assert audit.metadata["training_eligible"] is False


def test_later_scale_caps_are_explicit_but_not_runtime_authorization() -> None:
    assert P05_POSITIVE_SLOT_CAP == 0.10
    assert P08_POSITIVE_SLOT_CAP == 0.10
    assert MAX_VARIANTS_PER_SOURCE_PER_FAMILY == 1


def test_rule_code_remains_narrower_than_disabled_v2_design_contract() -> None:
    portfolio = yaml.safe_load((_ROOT / "configs/transformations/v2.yaml").read_text())
    families = {family["family_id"]: family for family in portfolio["families"]}
    assert portfolio["status"] == "design_only"
    for family_id in ("p05_resolved_names", "p08_type_ascriptions"):
        family = families[family_id]
        assert family["polarity"] == "positive"
        assert family["status"] == "disabled"
        assert family["implementation_status"] == "design_only"
        assert family["evidence_class"] == "E0"
        assert family["intended_relation"] == "equivalent"
        assert family["executable"] is False
        assert family["draft_emission_authorized"] is False
        assert family["label_emission_authorized"] is False

    assert set(families["p05_resolved_names"]["excluded_cases"]) == {
        "aliases",
        "ambiguous suffixes",
        "macro-generated names",
        "private names",
        "shadowed names",
    }
    assert set(families["p08_type_ascriptions"]["excluded_cases"]) == {
        "binder declarations",
        "coercion syntax owned by P07",
        "metavariable-containing types",
        "non-source-printable inferred types",
    }

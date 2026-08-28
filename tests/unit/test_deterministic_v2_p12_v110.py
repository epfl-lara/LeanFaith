"""Versioned P12 v1.1 complex root proof-arrow expansion."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from leanfaith.config.hashing import hash_file
from leanfaith.representations import alpha_identity_fingerprint
from leanfaith.schemas import CANONICAL_VIEW_NAMES, ViewStatus, make_id
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms.positives.v2_e0 import (
    P12ProofArrowBinderV110Rule,
    apply_presentation_trace,
    enumerate_p12_sites,
    enumerate_p12_v110_sites,
)
from leanfaith.transforms.v2_e0_runtime import (
    build_v2_e0_runtime,
    load_v2_e0_execution_config,
)
from tests.unit.record_factories import representation_record, theorem_record

_PROFILE = Path("configs/transformations/v2_e0_p12_v110_experimental.yaml")


def _source_records(source: str, key: str) -> tuple[TheoremRecord, RepresentationRecord]:
    tree = {
        "root": {
            "k": "forall",
            "bi": "default",
            "dom": {"k": "sort", "u": "0"},
            "body": {"k": "const", "n": "True", "us": []},
        }
    }
    theorem = theorem_record(
        theorem_id=make_id("thm", {"p12_v110": key}),
        ancestry_id=make_id("anc", {"p12_v110": key}),
        root_ancestry_ids=(make_id("anc", {"p12_v110": key}),),
        declaration_name=key,
        declaration_full_name=key,
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
        representation_id=make_id("repr", {"p12_v110": key}),
        theorem_id=theorem.theorem_id,
        raw_proof_stripped=source,
        headless=source.split(":", 1)[-1].split(":=", 1)[0],
        signature_pp="fixture type",
        signature_explicit="fixture explicit type",
        semantic_atoms=("const:Eq", "const:True"),
        operator_tree=tree,
        alpha_identity_fingerprint=alpha_identity_fingerprint(tree),
        view_status=statuses,
    )
    return theorem, representation


def test_p12_v110_profile_is_separate_and_does_not_change_v100() -> None:
    legacy = build_v2_e0_runtime()
    expanded = build_v2_e0_runtime(path=_PROFILE)
    loaded = load_v2_e0_execution_config(path=_PROFILE)

    assert legacy.rule_ids == ("p11_bounded_quantifiers", "p12_proof_arrow_binder")
    assert expanded.rule_ids == ("p12_proof_arrow_binder",)
    assert legacy.generation_config_hash != expanded.generation_config_hash
    assert legacy.portfolio_hash == expanded.portfolio_hash
    assert loaded.config.active_rules[0].rule_version == "1.1.0"
    assert hash_file(Path("configs/transformations/v2_e0_lf032_experimental.yaml")) == (
        "06325ec0ea185d5b95464bf0b468b9b10ddb5c4d3ba2a635d2fa7c986c29ff97"
    )
    assert (
        load_v2_e0_execution_config(
            path=Path("configs/transformations/v2_e0_lf032_experimental.yaml")
        ).config_hash
        == "d5131657b9b3d06a1b0f97666ccd3e83fce0567f5e525727d3514173aef80ee1"
    )

    source = "theorem rich (x : Nat) : x = 0 → True := by sorry"
    assert enumerate_p12_sites(source) == ()
    assert len(enumerate_p12_v110_sites(source)) == 1


def test_p12_v110_profile_and_addendum_replay_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = load_v2_e0_execution_config(path=_PROFILE)
    second = load_v2_e0_execution_config(path=_PROFILE)

    assert first.config_hash == second.config_hash
    assert first.config.model_dump(mode="json") == second.config.model_dump(mode="json")
    assert (
        first.config.portfolio_config_hash
        == first.config.model_dump(mode="json")["portfolio_config_hash"]
    )

    monkeypatch.setattr(
        "leanfaith.transforms.v2_e0_runtime.hash_file",
        lambda _path: "0" * 64,
    )
    with pytest.raises(ValueError, match="addendum byte hash changed"):
        load_v2_e0_execution_config(path=_PROFILE)


@pytest.mark.parametrize(
    "source",
    [
        "theorem data : Nat → Nat := by sorry",
        "theorem dependent : ∀ n : Nat, n = 0 → True := by sorry",
        "theorem nested (P Q R : Prop) : P ∧ (Q → R) := by sorry",
        "theorem used (x : Nat) : (_h : x = 0) → _h = _h := by sorry",
        "theorem dataBinder : (_n : Nat) → True := by sorry",
        "theorem decorated : ¬∃ f : Nat →+ Nat, True := by sorry",
        "theorem control : let p := True; p → p := by sorry",
        "theorem boolEq (a b : Nat) : (a == b) → True := by sorry",
        "theorem pipeline (f : Nat → Nat) (x : Nat) : (f <| x) → True := by sorry",
        "theorem nestedDomain (x : Nat) : (x = 0 → True) → True := by sorry",
        "theorem nestedNoSpace : (True→True) → True := by sorry",
        "theorem nestedConj (x : Nat) : (x = 0 ∧ (True → True)) → True := by sorry",
        "theorem nestedBinder (x : Nat) : (_h : x = 0 → True) → True := by sorry",
        "theorem decoratedDomain (f g : Nat) : (f →+ g) = f → True := by sorry",
    ],
)
def test_p12_v110_fails_closed_on_nonproof_or_nonroot_arrows(source: str) -> None:
    assert enumerate_p12_v110_sites(source) == ()


@pytest.mark.parametrize(
    "domain",
    [
        "x = 0",
        "0 < x ∧ x ≤ 10",
        "x ∈ s",
        "¬ x = 0",
        "True",
    ],
)
def test_p12_v110_complex_prop_forward_inverse_is_exact(domain: str) -> None:
    source = f"theorem rich (x : Nat) (s : Set Nat) : {domain} → True := by sorry"
    theorem, representation = _source_records(source, domain)
    rule = P12ProofArrowBinderV110Rule(
        generation_config_hash="a" * 64,
        candidate_pool="p12_v110_fixture",
    )

    draft = rule.generate(theorem, representation, 17)[0]

    assert draft.rule_version == "1.1.0"
    assert draft.candidate_code != source
    assert draft.inverse_trace is not None
    assert apply_presentation_trace(draft.candidate_code, draft.inverse_trace) == source
    assert draft.metadata["generation_intention_only"] is True


def test_p12_v110_named_proof_binder_reverse_requires_unused_binder() -> None:
    source = "theorem reverse (x : Nat) : (_h : x = 0) → True := by sorry"
    (site,) = enumerate_p12_v110_sites(source)
    assert site.operation == "binder_to_arrow_v110"
    assert site.replacement_text == " (x = 0) →"

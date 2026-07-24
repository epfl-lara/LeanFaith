"""LF-017 P01 capture-avoiding alpha-renaming tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from leanfaith.config.paths import find_repo_root
from leanfaith.schemas import CANONICAL_VIEW_NAMES, ViewStatus
from leanfaith.transforms.p01_alpha import (
    P01AlphaError,
    P01AlphaRule,
    load_p01_alpha_config,
    replay_inverse_trace,
)
from leanfaith.transforms.registry import TransformationRegistry, load_transformation_registry
from tests.unit.record_factories import representation_record, theorem_record

_ALPHA = "a" * 64


def _records(source: str):
    theorem = theorem_record(
        declaration_name="alpha_fixture",
        proof_stripped_declaration=source,
    )
    statuses = {
        name: (
            ViewStatus.OK
            if name
            in {
                "raw_proof_stripped",
                "headless",
                "semantic_atoms",
                "signature_pp",
            }
            else ViewStatus.NOT_ATTEMPTED
        )
        for name in CANONICAL_VIEW_NAMES
    }
    representation = representation_record(
        raw_proof_stripped=source,
        headless=source,
        semantic_atoms=("const:Eq", "forall"),
        alpha_identity_fingerprint=_ALPHA,
        view_status=statuses,
    )
    return theorem, representation


def _rule() -> P01AlphaRule:
    return P01AlphaRule.from_repository(generation_config_hash="b" * 64)


def test_config_is_strict_and_code_owned() -> None:
    loaded = load_p01_alpha_config()

    assert loaded.config.rule_id == "p01_alpha"
    assert loaded.config.implementation_key == P01AlphaRule.implementation_key
    assert loaded.config.supported_binder_kinds == ("explicit", "implicit", "instance")


def test_same_seed_is_byte_deterministic_and_inverse_is_exact() -> None:
    source = (
        "theorem alpha_fixture {α : Type} [inst : Inhabited α] (x y : α) "
        ": x = y → x = y := by sorry"
    )
    theorem, representation = _records(source)
    rule = _rule()

    first = rule.generate(theorem, representation, 17)[0]
    replay = rule.generate(theorem, representation, 17)[0]
    changed_seed = rule.generate(theorem, representation, 18)[0]

    assert first.model_dump(mode="json") == replay.model_dump(mode="json")
    assert changed_seed.draft_id != first.draft_id
    assert changed_seed.candidate_code != first.candidate_code
    assert first.intended_relation.value == "equivalent"
    assert first.intended_error_types == ()
    assert first.inverse_trace is not None
    assert replay_inverse_trace(first.candidate_code, first.inverse_trace) == source
    assert first.candidate_code.startswith("theorem alpha_fixture ")
    assert first.candidate_code.endswith(" := by sorry")
    assert "gold" not in first.model_dump_json()


def test_code_owned_implementation_registers_and_executes() -> None:
    loaded = load_transformation_registry()
    runtime = TransformationRegistry(loaded)
    runtime.register(P01AlphaRule.from_repository(generation_config_hash=loaded.registry_hash))
    source = "theorem alpha_fixture (x : Nat) : x = x := by sorry"
    theorem, representation = _records(source)

    execution = runtime.execute("p01_alpha", theorem, representation, 9)

    assert execution.attempt.terminal_outcome == "generated"
    assert len(execution.drafts) == 1
    assert execution.drafts[0].generation_config_hash == loaded.registry_hash


def test_seed_selects_from_explicit_implicit_and_instance_binders() -> None:
    source = "theorem alpha_fixture {α : Type} [inst : Inhabited α] (x y : α) : x = y := by sorry"
    theorem, representation = _records(source)
    rule = _rule()
    seen: set[str] = set()

    for seed in range(100):
        draft = rule.generate(theorem, representation, seed)[0]
        trace = draft.transformation_trace[0]
        seen.add(str(trace["binder_kind"]))

    assert seen == {"explicit", "implicit", "instance"}


def test_fresh_identifier_avoids_every_existing_identifier() -> None:
    rule = _rule()
    base = "theorem alpha_fixture (x : Nat) : x = x := by sorry"
    theorem, representation = _records(base)
    first = rule.generate(theorem, representation, 41)[0]
    colliding_name = str(first.transformation_trace[0]["to_identifier"])
    collision_source = (
        f"theorem alpha_fixture (x : Nat) : x = x ∧ {colliding_name} = {colliding_name} := by sorry"
    )
    theorem, representation = _records(collision_source)

    second = rule.generate(theorem, representation, 41)[0]

    assert second.transformation_trace[0]["to_identifier"] != colliding_name
    assert colliding_name in second.candidate_code


def test_nested_quantifier_shadowing_does_not_rename_inner_local() -> None:
    source = "theorem alpha_fixture (x : Nat) : (∀ x : Nat, x = x) ∧ x = x := by sorry"
    theorem, representation = _records(source)

    draft = _rule().generate(theorem, representation, 0)[0]
    new_name = str(draft.transformation_trace[0]["to_identifier"])

    assert f"(∀ x : Nat, x = x) ∧ {new_name} = {new_name}" in draft.candidate_code
    assert draft.candidate_code.count(new_name) == 3
    assert draft.inverse_trace is not None
    assert replay_inverse_trace(draft.candidate_code, draft.inverse_trace) == source


def test_lambda_shadowing_does_not_rename_inner_local() -> None:
    source = "theorem alpha_fixture (x : Nat) : (fun x : Nat => x + 1) x = x + 1 := by sorry"
    theorem, representation = _records(source)

    draft = _rule().generate(theorem, representation, 0)[0]
    new_name = str(draft.transformation_trace[0]["to_identifier"])

    assert f"(fun x : Nat => x + 1) {new_name} = {new_name} + 1" in draft.candidate_code


def test_set_builder_shadowing_does_not_rename_inner_local() -> None:
    source = "theorem alpha_fixture (x : Nat) : x ∈ {x | x > 0} ∨ x = x := by sorry"
    theorem, representation = _records(source)

    draft = _rule().generate(theorem, representation, 0)[0]
    new_name = str(draft.transformation_trace[0]["to_identifier"])

    assert f"{new_name} ∈ {{x | x > 0}} ∨ {new_name} = {new_name}" in draft.candidate_code


def test_later_command_binder_shadows_selected_name_after_its_type() -> None:
    source = "theorem alpha_fixture (x : Nat) (x : Fin (x + 1)) : x.1 < x := by sorry"
    theorem, representation = _records(source)
    rule = _rule()

    candidates = [rule.generate(theorem, representation, seed)[0] for seed in range(20)]
    outer = next(
        draft for draft in candidates if draft.transformation_trace[0]["binder_ordinal"] == 0
    )
    new_name = str(outer.transformation_trace[0]["to_identifier"])

    assert f"(x : Fin ({new_name} + 1))" in outer.candidate_code
    assert ": x.1 < x" in outer.candidate_code


def test_comments_strings_qualified_suffixes_and_named_labels_are_not_rewritten() -> None:
    source = (
        "theorem alpha_fixture (x : Nat) : "
        "(\"-- x\".length = 4) ∧ ('x' = 'x') ∧ "
        "Nat.succ x = Nat.succ (x := x) := by sorry"
    )
    theorem, representation = _records(source)

    draft = _rule().generate(theorem, representation, 2)[0]
    new_name = str(draft.transformation_trace[0]["to_identifier"])

    assert '"-- x"' in draft.candidate_code
    assert "'x' = 'x'" in draft.candidate_code
    assert "(x :=" in draft.candidate_code
    assert f"(x := {new_name})" in draft.candidate_code


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ("theorem alpha_fixture : True := by sorry", "no_eligible_binder"),
        (
            "theorem alpha_fixture (x : Nat) : (match x with | 0 => True | _ => False) := by sorry",
            "unsupported_scope_match",
        ),
        (
            "theorem alpha_fixture (x : Nat) : x = x := by exact rfl",
            "unsupported_proof_placeholder",
        ),
    ],
)
def test_unsupported_or_ambiguous_sources_fail_explicitly(source: str, reason: str) -> None:
    theorem, representation = _records(source)

    result = _rule().assess(theorem, representation)

    assert not result.applicable
    assert result.reason_codes == (reason,)
    with pytest.raises(P01AlphaError, match="non-applicable"):
        _rule().generate(theorem, representation, 0)


def test_source_representation_mismatch_fails_explicitly() -> None:
    theorem, representation = _records("theorem alpha_fixture (x : Nat) : x = x := by sorry")
    representation = representation.model_copy(
        update={"raw_proof_stripped": "theorem other (x : Nat) : x = x := by sorry"}
    )

    result = _rule().assess(theorem, representation)

    assert result.reason_codes == ("source_representation_mismatch",)


def test_golden_fixture_replays() -> None:
    root = find_repo_root(Path(__file__).parent)
    fixture = json.loads(
        (root / "tests/golden/transforms/p01_alpha_v1.json").read_text(encoding="utf-8")
    )
    theorem, representation = _records(fixture["source"])

    draft = _rule().generate(theorem, representation, fixture["seed"])[0]

    assert draft.candidate_code == fixture["candidate"]
    assert draft.transformation_trace == (fixture["trace"],)
    assert draft.inverse_trace is not None
    assert replay_inverse_trace(draft.candidate_code, draft.inverse_trace) == fixture["source"]

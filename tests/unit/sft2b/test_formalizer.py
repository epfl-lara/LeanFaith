from __future__ import annotations

from pathlib import Path

from leanfaith.config.paths import find_repo_root
from leanfaith.sft2b.formalizer import (
    extract_final_theorem_signature,
    extract_proposition,
    load_formalizer_config,
)

_REPO_ROOT = find_repo_root(Path(__file__).parent)


def test_reform_snapshot_prompt_slots_and_decoding_are_fully_pinned() -> None:
    config = load_formalizer_config(
        _REPO_ROOT, _REPO_ROOT / "configs/sft2b/reform_8b_smoke_v1.json"
    )

    assert config.model_revision == "1589c832cfad679a280b222e694b987a33befd26"
    assert len(config.snapshot_files) == 16
    assert [item.seed for item in config.slots] == [0, 1, 2, 3]
    assert config.decoding["max_new_tokens"] == 4096


def test_strict_formalizer_extraction_accepts_only_one_proof_free_proposition() -> None:
    value, failure = extract_proposition(
        "reasoning\n<<<LEAN_PROPOSITION>>>\n∀ (n : Nat), n = n\n<<<END_LEAN_PROPOSITION>>>\n"
    )
    assert value == "∀ (n : Nat), n = n"
    assert failure is None

    for invalid in (
        "<<<LEAN_PROPOSITION>>>\ntheorem x : True\n<<<END_LEAN_PROPOSITION>>>",
        "<<<LEAN_PROPOSITION>>>\nTrue := by trivial\n<<<END_LEAN_PROPOSITION>>>",
        "<<<LEAN_PROPOSITION>>>\n∀ n, n = n\n<<<END_LEAN_PROPOSITION>>>\nextra",
        "no markers",
    ):
        value, failure = extract_proposition(invalid)
        assert value is None
        assert failure is not None


def test_native_reform_declaration_is_reduced_to_a_proof_free_closed_term() -> None:
    value, failure = extract_final_theorem_signature(
        "reflection\n</think>\nfinal:\n```lean4\nimport Mathlib\n"
        "theorem sft2b_candidate {α : Type*} (x : α) : x = x := by sorry\n```\n"
    )
    assert value == "∀ {α : Type sft2b_u_0} (x : α), x = x"
    assert failure is None

    for invalid in (
        "</think>\n```lean4\ntheorem x : True := by exact True.intro\n```",
        "</think>\n```lean4\ntheorem x : True := by sorry\n```\nextra",
        "</think>\n```lean4\ntheorem x : True := by sorry\n```\n```lean4\nTrue\n```",
    ):
        value, failure = extract_final_theorem_signature(invalid)
        assert value is None
        assert failure is not None


def test_v2_reform_config_pins_native_declaration_extraction() -> None:
    config = load_formalizer_config(
        _REPO_ROOT, _REPO_ROOT / "configs/sft2b/reform_8b_theorem_smoke_v2.json"
    )
    assert config.extraction_contract == "final_theorem_signature_v1"

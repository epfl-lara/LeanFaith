from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from leanfaith.config.hashing import hash_file
from leanfaith.sft2a.config import load_sft2a_config
from leanfaith.sft2a.lean_oracle import _signature_command, compile_context
from leanfaith.sft2a.models import ProposerOutput
from leanfaith.sft2a.prompts import render_blinded_judge_prompt


def test_frozen_config_pins_repr_prompts_schemas_and_gold_blocklist() -> None:
    loaded = load_sft2a_config()

    assert loaded.config.repr.freeze_commit == "176a783842c5a73b84413dfa8347670608b615d9"
    assert (
        loaded.config.repr.spec_hash
        == "68d893a2c566bf3f6a82c899a32a351f9a5420f5ea98168c99b887aaa01a45a8"
    )
    assert (
        loaded.config.repr.implementation_set_hash
        == "9a9252fff5ffc69cb65e71120fedffa83ed47271aecadbecf0ceb890feea65ff"
    )
    for binding in (
        loaded.config.prompts.codex_proposer,
        loaded.config.prompts.blinded_claude_judge,
        loaded.config.schemas.codex_proposer_output,
        loaded.config.schemas.blinded_judge_output,
    ):
        assert hash_file(loaded.repo_root / binding.path) == binding.sha256
    assert hash_file(loaded.repo_root / loaded.config.gold_screen.path) == (
        loaded.config.gold_screen.sha256
    )


@pytest.mark.parametrize("placeholder", ["[anonymous]", "⋯", "..."])
def test_proposer_schema_rejects_placeholders(placeholder: str) -> None:
    with pytest.raises(ValidationError):
        ProposerOutput(
            schema_version=1,
            requested_polarity="preserving",
            mechanism="other",
            candidate_signature=f"True ∧ {placeholder}",
            change_summary="test",
            judge_trap="test",
            informative=True,
            proof_free=True,
        )


def test_judge_prompt_is_blinded_to_slot_and_polarity() -> None:
    loaded = load_sft2a_config()
    prompt = render_blinded_judge_prompt(
        loaded,
        statement_a="α : Type\n⊢ True",
        statement_b="β : Type\n⊢ False",
    )

    assert "preserve_0" not in prompt
    assert "breaking" not in prompt
    assert "requested polarity" not in prompt.casefold()
    assert "α : Type\n⊢ True" in prompt
    assert "β : Type\n⊢ False" in prompt


def test_judge_prompt_accepts_adjacent_lean_set_braces_from_crashed_root() -> None:
    loaded = load_sft2a_config(Path("configs/sft2a/closure_aware_v5_2_recovery_v3.yaml"))
    reference = (
        "𝕜 : Type u_0\ninst✝² : NontriviallyNormedField 𝕜\nE : Type u_1\n"
        "inst✝¹ : NormedAddCommGroup E\ninst✝ : NormedSpace 𝕜 E\nf : 𝕜 → E\nx : 𝕜\n"
        "⊢ Filter.EventuallyConst f (nhdsWithin x {x}ᶜ) ↔ "
        "∃ c, meromorphicOrderAt (fun x => f x - c) x = ⊤"
    )
    candidate = (
        "𝕜 : Type\ninst : NontriviallyNormedField 𝕜\nE : Type\n"
        "inst_1 : NormedAddCommGroup E\ninst_2 : NormedSpace 𝕜 E\nf : 𝕜 → E\nx : 𝕜\n"
        "⊢ EventuallyConst f (𝓝[{y | y ∉ {x}}] x) ↔ "
        "∃ c, meromorphicOrderAt (fun y => f y - c) x = ⊤"
    )

    prompt = render_blinded_judge_prompt(
        loaded,
        statement_a=reference,
        statement_b=candidate,
    )

    assert "𝓝[{y | y ∉ {x}}] x" in prompt
    assert "{{STATEMENT_A}}" not in prompt
    assert "{{STATEMENT_B}}" not in prompt


def test_signature_oracle_elaborates_once_and_renders_same_expr_without_proof() -> None:
    loaded = load_sft2a_config()
    command = _signature_command(
        context=compile_context(loaded),
        signature="True",
        endpoint_id="test:endpoint",
        render_scope_id="test:scope",
    )

    assert command.count("Term.elabTerm") == 1
    assert command.count("LeanFaith.GoalV1.emitClosedProp") == 1
    assert command.count("canonicalizeBinderMetadata #[] proposition") == 1
    oracle_definition = command.split("namespace LeanFaith.SFT2A.SignatureOracle", maxsplit=1)[1]
    assert "let proposition ← Term.elabTerm" in oracle_definition
    assert "let proposition := canonicalizeBinderMetadata #[] proposition" in oracle_definition
    assert "let structuralArrow :=" in oracle_definition
    assert "!body.hasLooseBVar 0" in oracle_definition
    assert command.rstrip().splitlines()[-1].startswith("lfSft2aSignature ")
    assert "emitClosedProp\n      endpoint.getString" in oracle_definition
    assert ":= by" not in oracle_definition
    assert "sorry" not in oracle_definition
    assert "axiom" not in oracle_definition
    assert "admit" not in oracle_definition
    project_dir = Path(loaded.config.root.compile_context.project_dir)
    assert project_dir.is_dir()
    assert (
        (project_dir / "lean-toolchain")
        .read_text(encoding="utf-8")
        .strip()
        .endswith(loaded.config.root.compile_context.lean_version)
    )

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from leanfaith.config.hashing import sha256_hex
from leanfaith.generation.prompts import (
    DEFAULT_DIRECT_AUTOFORMALIZATION_TEMPLATE,
    DirectOutputErrorCode,
    DirectOutputParseError,
    DirectPromptError,
    DirectPromptErrorCode,
    PublicTrustedProblem,
    parse_direct_autoformalization_output,
    render_direct_autoformalization_prompt,
)
from leanfaith.schemas.enums import NLTrust


def trusted_problem(**overrides: object) -> PublicTrustedProblem:
    values: dict[str, object] = {
        "problem_record_id": "problem:" + "1" * 64,
        "problem_id": "public-problem-1",
        "problem_group": "grp:public-problem-1",
        "nl_statement": "Prove that every natural number equals itself.",
        "nl_source_link": "https://example.test/public-problems/1",
        "nl_trust": NLTrust.TRUSTED,
        "source_id": "public/example",
        "source_revision": "rev-1",
        "source_license": "CC-BY-4.0",
        "source_is_public": True,
        "external_transmission_allowed": True,
        "denylist_checked": True,
        "denylist_hits": (),
    }
    values.update(overrides)
    return PublicTrustedProblem(**values)  # type: ignore[arg-type]


def test_renderer_is_deterministic_and_binds_exact_template_hash() -> None:
    problem = trusted_problem()
    first = render_direct_autoformalization_prompt(problem)
    second = render_direct_autoformalization_prompt(problem)

    template_bytes = DEFAULT_DIRECT_AUTOFORMALIZATION_TEMPLATE.read_bytes()
    assert first == second
    assert first.template_id == "direct_autoformalize"
    assert first.template_version == "v1"
    assert first.template_sha256 == sha256_hex(template_bytes)
    assert first.template_sha256 in first.text
    assert first.render_sha256 == sha256_hex(first.text.encode("utf-8"))
    assert "{{" not in first.text

    payload_text = first.text.rstrip().rsplit("\n", maxsplit=1)[-1]
    payload = json.loads(payload_text)
    assert payload["problem_record_id"] == problem.problem_record_id
    assert payload["nl_statement"] == problem.nl_statement
    assert payload["nl_trust"] == "trusted"
    assert payload["source_is_public"] is True
    assert payload["external_transmission_allowed"] is True


def test_renderer_hash_changes_with_problem_content() -> None:
    first = render_direct_autoformalization_prompt(trusted_problem())
    second = render_direct_autoformalization_prompt(
        trusted_problem(nl_statement="Prove that zero is a natural number.")
    )
    assert first.template_sha256 == second.template_sha256
    assert first.render_sha256 != second.render_sha256


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"problem_record_id": "not-an-id"}, DirectPromptErrorCode.INVALID_PROBLEM),
        ({"nl_statement": "  "}, DirectPromptErrorCode.INVALID_PROBLEM),
        ({"nl_trust": NLTrust.SYNTHETIC}, DirectPromptErrorCode.UNTRUSTED_NL),
        ({"nl_trust": NLTrust.UNCERTAIN}, DirectPromptErrorCode.UNTRUSTED_NL),
        ({"source_is_public": False}, DirectPromptErrorCode.PRIVATE_SOURCE),
        (
            {"external_transmission_allowed": False},
            DirectPromptErrorCode.EXTERNAL_TRANSMISSION_FORBIDDEN,
        ),
        ({"denylist_checked": False}, DirectPromptErrorCode.DENYLIST_NOT_CLEARED),
        ({"denylist_hits": ("benchmark:x",)}, DirectPromptErrorCode.DENYLIST_NOT_CLEARED),
    ],
)
def test_renderer_fails_closed_for_ineligible_problem(
    overrides: dict[str, object],
    code: DirectPromptErrorCode,
) -> None:
    with pytest.raises(DirectPromptError) as caught:
        render_direct_autoformalization_prompt(trusted_problem(**overrides))
    assert caught.value.code is code


@pytest.mark.parametrize(
    "template",
    [
        "no placeholders",
        "{{PROMPT_TEMPLATE_SHA256}}\n",
        "{{PROBLEM_JSON}}\n",
        "{{PROMPT_TEMPLATE_SHA256}}\n{{PROBLEM_JSON}}\n{{UNKNOWN}}\n",
        "{{PROMPT_TEMPLATE_SHA256}}\n{{PROMPT_TEMPLATE_SHA256}}\n{{PROBLEM_JSON}}\n",
    ],
)
def test_renderer_rejects_template_contract_drift(tmp_path: Path, template: str) -> None:
    path = tmp_path / "template.txt"
    path.write_text(template, encoding="utf-8")
    with pytest.raises(DirectPromptError) as caught:
        render_direct_autoformalization_prompt(trusted_problem(), template_path=path)
    assert caught.value.code is DirectPromptErrorCode.TEMPLATE_CONTRACT


@pytest.mark.parametrize("label", ["lean", "lean4", "LEAN4"])
def test_parser_extracts_one_proof_free_declaration(label: str) -> None:
    raw = f"```{label}\ntheorem generated_identity (n : Nat) :\n  n = n\n```\n"
    parsed = parse_direct_autoformalization_output(raw)
    assert parsed.declaration_kind == "theorem"
    assert parsed.declaration_name == "generated_identity"
    assert parsed.statement == "theorem generated_identity (n : Nat) :\n  n = n"
    assert parsed.statement_sha256 == sha256_hex(parsed.statement.encode("utf-8"))
    assert not hasattr(parsed, "raw_output")
    assert {field.name for field in dataclasses.fields(parsed)} == {
        "declaration_kind",
        "declaration_name",
        "statement",
        "statement_sha256",
    }


def test_parser_accepts_named_lemma() -> None:
    parsed = parse_direct_autoformalization_output("```lean4\nlemma l : True\n```")
    assert parsed.declaration_kind == "lemma"
    assert parsed.declaration_name == "l"


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("", DirectOutputErrorCode.EMPTY_OUTPUT),
        ("plain Lean text", DirectOutputErrorCode.MISSING_FENCE),
        ("```lean4\n```", DirectOutputErrorCode.EMPTY_DECLARATION),
        ("```python\ntheorem t : True\n```", DirectOutputErrorCode.WRONG_FENCE_LANGUAGE),
        ("```lean4\ntheorem t : True", DirectOutputErrorCode.MALFORMED_FENCE),
        (
            "```lean4\ntheorem t : True\n```\n```lean4\ntheorem u : True\n```",
            DirectOutputErrorCode.MULTIPLE_FENCES,
        ),
        (
            "Explanation\n```lean4\ntheorem t : True\n```",
            DirectOutputErrorCode.MISSING_FENCE,
        ),
        (
            "```lean4\ntheorem t : True\n```\nExplanation",
            DirectOutputErrorCode.EXTRA_TEXT,
        ),
        (
            "```lean4\n-- generated\ntheorem t : True\n```",
            DirectOutputErrorCode.COMMENTARY_IN_FENCE,
        ),
        ("```lean4\nexample : True\n```", DirectOutputErrorCode.UNSUPPORTED_DECLARATION),
        ("```lean4\ntheorem t\n```", DirectOutputErrorCode.MISSING_TYPE),
        (
            "```lean4\ntheorem t : True\nlemma u : True\n```",
            DirectOutputErrorCode.MULTIPLE_DECLARATIONS,
        ),
    ],
)
def test_parser_fails_closed_on_malformed_or_ambiguous_output(
    raw: str,
    code: DirectOutputErrorCode,
) -> None:
    with pytest.raises(DirectOutputParseError) as caught:
        parse_direct_autoformalization_output(raw)
    assert caught.value.code is code


@pytest.mark.parametrize(
    "statement",
    [
        "theorem t : True := by trivial",
        "theorem t : True := sorry",
        "theorem t : True by trivial",
        "theorem t : True\nwhere\n  helper := True",
        "theorem t : True admit",
    ],
)
def test_parser_rejects_proof_bearing_output(statement: str) -> None:
    with pytest.raises(DirectOutputParseError) as caught:
        parse_direct_autoformalization_output(f"```lean4\n{statement}\n```")
    assert caught.value.code is DirectOutputErrorCode.PROOF_BEARING_OUTPUT

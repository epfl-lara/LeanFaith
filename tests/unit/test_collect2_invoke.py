"""Exact local prompt profiles and robust CLI-tail extraction for collect2."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from leanfaith.collect2.invoke import (
    AutoformalizationTask,
    InvocationSession,
    ProviderSpec,
    parse_cli_json_tail,
    render_task,
    resolve_local_profile,
)

_SUFFIX = (
    "Return the final answer as one Lean 4 theorem or lemma declaration in one final Markdown "
    "fence labelled `lean4`. Use the registered theorem name. Do not invent a different import "
    "context. Explanatory reasoning may precede the final fence, but return no second Lean fence "
    "or alternative declaration."
)


def _task() -> AutoformalizationTask:
    return AutoformalizationTask(
        problem_id="fixture",
        nl_statement="Show that one plus one is two.",
        header="import Mathlib\nopen Nat",
        theorem_name="fixture_theorem",
    )


def test_goedel_prompt_is_byte_exact() -> None:
    expected_user = (
        "Please autoformalize the following natural language problem statement in Lean 4. Use the "
        "following theorem name: fixture_theorem\n"
        "The natural language statement is: \n"
        "Show that one plus one is two.Think before you provide the lean statement.\n\n"
        f"{_SUFFIX}\n"
    )
    rendered = render_task(_task(), ProviderSpec(kind="local_hf", model="goedel"))
    assert rendered.user_prompt == expected_user
    assert rendered.prompt == (
        f"<|im_start|>user\n{expected_user}<|im_end|>\n<|im_start|>assistant\n"
    )
    assert hashlib.sha256(resolve_local_profile("goedel").user_template.encode()).hexdigest() == (
        "1fb4e6972c27c0a937a35913a75b1c705412416009f38a204b8824cd7ccb04c3"
    )


def test_kimina_prompt_is_byte_exact() -> None:
    expected_user = (
        "Please autoformalize the following problem in Lean 4 with a header. Use the following "
        "theorem names: fixture_theorem.\n\n"
        "Show that one plus one is two.\n\n"
        "The registered Lean header is:\n"
        "import Mathlib\nopen Nat\n\n"
        f"{_SUFFIX}\n"
    )
    rendered = render_task(_task(), ProviderSpec(kind="local_hf", model="kimina"))
    assert rendered.user_prompt == expected_user
    assert rendered.prompt == (
        "<|im_start|>system\nYou are an expert in mathematics and Lean 4.<|im_end|>\n"
        f"<|im_start|>user\n{expected_user}<|im_end|>\n<|im_start|>assistant\n"
    )
    assert hashlib.sha256(resolve_local_profile("kimina").user_template.encode()).hexdigest() == (
        "d9396d6688e2a21059f7f359f46c10e9cf5ee58dd2332cc796217b8ac1975e4b"
    )


def test_stepfun_prompt_is_byte_exact() -> None:
    expected_user = (
        "Please autoformalize the following problem in Lean 4 with a header. Use the following "
        "theorem names: fixture_theorem.\n\n"
        "Show that one plus one is two.\n\n"
        "Your code should start with:\n"
        "```Lean4\n"
        "import Mathlib\nopen Nat\n"
        "```\n\n"
        f"{_SUFFIX}\n"
    )
    rendered = render_task(_task(), ProviderSpec(kind="local_hf", model="stepfun"))
    assert rendered.user_prompt == expected_user
    assert rendered.prompt == (
        "<｜begin▁of▁sentence｜>You are an expert in mathematics and Lean 4."
        f"<｜User｜>{expected_user}<｜Assistant｜><think>"
    )
    assert hashlib.sha256(resolve_local_profile("stepfun").user_template.encode()).hexdigest() == (
        "f33fe08c5bc09d6ad97deeac31121d687b2be4b27f0a77d0870ada3b2249e8c1"
    )


def test_cli_json_tail_prefers_last_schema_object_and_nested_agent_text() -> None:
    first = json.dumps({"candidate_lean": "theorem stale : False := by sorry"})
    final = json.dumps({"candidate_lean": "theorem fresh : True := by sorry"})
    assert parse_cli_json_tail(f"trace {first}\nmore trace\n{final}\n") == (
        "theorem fresh : True := by sorry"
    )

    nested = json.dumps(
        {
            "type": "item.completed",
            "item": {"text": json.dumps({"candidate_lean": "lemma nested : True"})},
        }
    )
    assert parse_cli_json_tail(nested) == "lemma nested : True"


def test_cli_invocation_mirrors_codex_exec_command() -> None:
    observed: list[object] = []

    def runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.extend(args)
        observed.append(kwargs)
        stdout = json.dumps({"candidate_lean": "theorem fixture_theorem : True"})
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")

    provider = ProviderSpec(
        kind="cli",
        cli="codex",
        model="gpt-5.6-sol",
        cwd=Path("/tmp"),
    )
    rendered = render_task(_task(), provider)
    with InvocationSession(provider, subprocess_runner=runner) as session:
        result = session.run(rendered)

    command = observed[0]
    assert isinstance(command, list)
    assert command[:6] == [
        "codex",
        "exec",
        "--dangerously-bypass-approvals-and-sandbox",
        "-c",
        'model_reasoning_effort="high"',
        "--skip-git-repo-check",
    ]
    assert command[-3:-1] == ["-m", "gpt-5.6-sol"]
    assert command[-1] == rendered.prompt
    assert result.candidate_output == "theorem fixture_theorem : True"

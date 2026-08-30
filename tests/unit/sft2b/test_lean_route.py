from __future__ import annotations

import re
from pathlib import Path

import pytest

from leanfaith.config.paths import find_repo_root
from leanfaith.sft2b.lean import (
    PropositionEndpoint,
    build_session_body,
    build_tolerant_session_body,
)

_REPO_ROOT = find_repo_root(Path(__file__).parent)
_HELPER = _REPO_ROOT / "src/leanfaith/sft2b/lean_helper.lean"


def _endpoints() -> tuple[PropositionEndpoint, PropositionEndpoint]:
    return (
        PropositionEndpoint(
            endpoint_id="reference",
            endpoint_role="reference",
            proposition="∀ {x : ℝ}, 0 ≤ x → x - x ^ 3 / 6 ≤ Real.sin x",
            source_id=f"sft2b_source:{'a' * 64}",
        ),
        PropositionEndpoint(
            endpoint_id="candidate",
            endpoint_role="candidate",
            proposition="∀ (x : ℝ), 0 ≤ x → x - x ^ 3 / 6 ≤ Real.sin x",
            source_id=f"sft2b_source:{'a' * 64}",
            candidate_id=f"sft2b_candidate:{'b' * 64}",
        ),
    )


def test_live_action_elaborates_each_endpoint_once_and_renders_same_expr() -> None:
    body = build_session_body(_endpoints(), render_scope_id="scope:smoke")

    assert body.count("run_meta do") == 1
    assert body.count("elaborateProposition") == 2
    assert body.count("LeanFaith.GoalV1.emitClosedProp") == 2
    assert "Term.elabTerm" not in body
    assert "theorem " not in body
    assert "sorry" not in body
    assert "axiom " not in body
    assert body.index("elaborateProposition") < body.index("emitClosedProp")


def test_static_helper_has_one_term_elaboration_and_no_proof_escape() -> None:
    source = _HELPER.read_text(encoding="utf-8")

    assert source.count("Term.elabTerm") == 1
    assert "def elaborateProposition" in source
    assert "checkedClosedProp origin e" in source
    assert ":= by" not in source
    assert re.search(r"(?m)^\s*axiom\s", source) is None
    assert re.search(r"\bsorry\b", source, flags=re.IGNORECASE) is None


def test_tolerant_batch_elaborates_reference_and_candidates_once_each() -> None:
    endpoints = (*_endpoints(),)
    body = build_tolerant_session_body(endpoints, render_scope_id="scope:batch")

    assert body.count("run_meta do") == 1
    assert body.count("elaborateProposition") == len(endpoints)
    assert body.count("LeanFaith.GoalV1.emitClosedProp") == len(endpoints)
    assert body.count("Lean.mkApp3") == len(endpoints) - 1
    assert "logInfo" not in body
    assert "Term.elabTerm" not in body
    assert "theorem " not in body
    assert "sorry" not in body
    assert ":= by" not in body


@pytest.mark.parametrize("forbidden", ["sorry", "axiom Foo : True", "True := by trivial"])
def test_proof_or_axiom_bearing_endpoint_is_rejected(forbidden: str) -> None:
    with pytest.raises(ValueError, match="proof-free"):
        PropositionEndpoint(
            endpoint_id="candidate",
            endpoint_role="candidate",
            proposition=forbidden,
            source_id=f"sft2b_source:{'a' * 64}",
            candidate_id=f"sft2b_candidate:{'b' * 64}",
        )

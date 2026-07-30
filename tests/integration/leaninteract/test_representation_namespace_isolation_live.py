"""Regression for helper namespace pollution in inline repr_v3 elaboration."""

from __future__ import annotations

import datetime
import shutil
from pathlib import Path

import pytest

from leanfaith.cli.pipeline import default_mathlib_checkout
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.representations.pipeline import TheoremForRepresentation, build_representations
from leanfaith.schemas.ids import make_id

PROJECT = default_mathlib_checkout()
_CTX_FP = "6" * 64
_CTX = f"ctx:{_CTX_FP}"
_UTC = datetime.datetime(2026, 7, 30, tzinfo=datetime.UTC)
_PROOF_SENTINEL = "LEANFAITH_LOG_NAMESPACE_PROOF_SENTINEL"

pytestmark = [
    pytest.mark.lean,
    pytest.mark.skipif(shutil.which("lake") is None, reason="Lean toolchain unavailable"),
    pytest.mark.skipif(
        not (PROJECT / "lean-toolchain").is_file(),
        reason="pinned mathlib checkout unavailable",
    ),
]


def _inline_log_theorem(name: str, *, qualified: bool) -> TheoremForRepresentation:
    log_name = "Real.log" if qualified else "log"
    statement = (
        f"theorem {name} {{f : ℝ → ℝ}} "
        f"(hf : f = fun x => {log_name} (2 + x) + {log_name} (2 - x)) :\n"
        "(∀ x ∈ Set.Ioo (-2) 2, f x = f (-x)) ∧ "
        "StrictAntiOn f (Set.Ioo 0 2)"
    )
    inline_source = "\n".join(
        (
            "import Mathlib",
            "open Real Set",
            "open scoped BigOperators",
            statement + " := by",
            f'  have proofOnly : String := "{_PROOF_SENTINEL}"',
            "  sorry",
        )
    )
    return TheoremForRepresentation(
        theorem_id=make_id("thm", {"namespace_isolation": name}),
        full_name=name,
        proof_stripped=statement + " := by sorry",
        inline_source=inline_source,
        context_id=_CTX,
        inline_declaration=True,
    )


def test_helper_namespace_does_not_ambiguate_unqualified_real_log(
    tmp_path: Path,
) -> None:
    backend = LeanInteractBackend(
        BackendSettings(
            project_dir=PROJECT,
            context_fingerprint=_CTX_FP,
            environment_schema_version=1,
            raw_response_dir=tmp_path / "lean_raw",
        )
    )
    try:
        unqualified = _inline_log_theorem("lf_repr_unqualified_log", qualified=False)
        qualified = _inline_log_theorem("lf_repr_qualified_log", qualified=True)
        unqualified_record, qualified_record = build_representations(
            backend,
            [unqualified, qualified],
            imports="import Mathlib",
            created_at=_UTC,
        )
    finally:
        backend.close()

    required_views = (
        "raw_proof_stripped",
        "headless",
        "signature_pp",
        "signature_explicit",
        "semantic_atoms",
        "operator_tree",
    )
    for record in (unqualified_record, qualified_record):
        assert all(record.view_status[view].value == "ok" for view in required_views)
        assert record.alpha_identity_fingerprint is not None
        assert record.semantic_atoms is not None
        assert "const:Real.log" in record.semantic_atoms
        assert "const:Lean.log" not in record.semantic_atoms
        assert _PROOF_SENTINEL not in record.model_dump_json()

    assert (
        unqualified_record.alpha_identity_fingerprint == qualified_record.alpha_identity_fingerprint
    )
    assert unqualified_record.signature_pp == qualified_record.signature_pp
    assert unqualified_record.signature_explicit == qualified_record.signature_explicit

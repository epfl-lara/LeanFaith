"""LF-012 live: proof-strip + re-elaboration against the real REPL (§12.4).

Submits a battery of diverse declaration shapes, strips each via ranges, and
confirms every stripped statement re-elaborates as VALID_WITH_SORRY (the real
proof-leak guard) and no longer proves without the placeholder.
"""

from __future__ import annotations

import datetime
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from leanfaith.config.paths import find_repo_root
from leanfaith.lean.extraction import (
    ExtractedDeclaration,
    SourceIdentity,
    build_theorem_record,
    extract_from_declarations,
    reconstruct_for_revalidation,
)
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanStatus
from leanfaith.schemas import ValidationStatus

pytestmark = [
    pytest.mark.lean,
    pytest.mark.skipif(shutil.which("lake") is None, reason="Lean toolchain unavailable"),
]

_FIXTURES = find_repo_root(Path(__file__).parent) / "tests" / "lean_fixtures"
_CTX_FP = "0" * 64
_CTX = f"ctx:{_CTX_FP}"
_UTC = datetime.datetime(2026, 7, 11, tzinfo=datetime.UTC)

# Diverse shapes (no mathlib needed): term/by/multiline/attrs/docstring/
# unicode/implicit+instance binders/where/def(non-prop).
_CASES = [
    "theorem c_term (n : Nat) : n = n := rfl",
    "theorem c_by (x y : Nat) : x + y = y + x := by omega",
    "theorem c_ml (x y : Nat) :\n    x + y = y + x := by\n  omega",
    "@[simp] theorem c_attr (n : Nat) : n + 0 = n := by omega",
    "/-- a documented lemma -/\ntheorem c_doc : True := trivial",
    "theorem c_imp {p : Prop} (h : p) : p := h",
    "theorem c_uni (α : Type) (a : α) : a = a := rfl",
    "theorem c_where (n : Nat) : n = n := by exact h where h : n = n := rfl",
    "def c_def (n : Nat) : Nat := n + 1",
    "theorem c_forall : ∀ n : Nat, n + 0 = n := by intro n; omega",
    "theorem c_exists : ∃ n : Nat, n = 0 := ⟨0, rfl⟩",
    "theorem c_le (x : Nat) : x ≤ x + 1 := by omega",
]


@pytest.fixture(scope="module")
def backend(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LeanInteractBackend]:
    instance = LeanInteractBackend(
        BackendSettings(
            project_dir=_FIXTURES,
            context_fingerprint=_CTX_FP,
            environment_schema_version=1,
            raw_response_dir=tmp_path_factory.mktemp("extract_raw"),
        )
    )
    yield instance
    instance.close()


def _declarations(backend: LeanInteractBackend, code: str) -> list[dict]:
    result = backend.run(
        LeanRequest(request_id=f"decls-{hash(code)}", context_id=_CTX, code=code, declarations=True)
    )
    assert result.status in (LeanStatus.VALID, LeanStatus.VALID_WITH_SORRY), result.status
    return list(result.declarations)


def _identity(record: str) -> SourceIdentity:
    return SourceIdentity(
        source="fixtures", source_revision="workspace", source_record=record, context_id=_CTX
    )


def test_every_shape_strips_and_revalidates(backend: LeanInteractBackend) -> None:
    stripped_count = 0
    for index, code in enumerate(_CASES):
        decls = _declarations(backend, code)
        result = extract_from_declarations(_identity(f"case-{index}"), code, decls, created_at=_UTC)
        # def is the only non-theorem case; everything else yields one theorem.
        if code.startswith("def "):
            assert result.accepted == ()
            continue
        assert len(result.accepted) == 1, code
        extracted = result.accepted[0]
        stripped_count += 1

        # 1. The proof is gone: re-elaborating strict must NOT be clean-valid.
        revalidation_code = reconstruct_for_revalidation(code, decls[0], extracted.proof_stripped)
        revalidated = backend.run(
            LeanRequest(
                request_id=f"reval-{index}",
                context_id=_CTX,
                code=revalidation_code,
                allow_sorry=True,
            )
        )
        assert revalidated.status == LeanStatus.VALID_WITH_SORRY, (code, revalidated.status)

        # 2. The stripped statement literally ends with the placeholder.
        assert extracted.proof_stripped.endswith(":= by sorry")
        # 3. No original proof tactic leaked into the statement text.
        for token in ("omega", "rfl", "trivial", "intro", "exact"):
            body = extracted.proof_stripped[: -len(" := by sorry")]
            assert token not in body, (code, token)
    assert stripped_count == len(_CASES) - 1  # all but the def


def test_extracted_records_are_wellformed(backend: LeanInteractBackend) -> None:
    code = "theorem well_formed (x y : Nat) : x + y + 0 = y + x := by omega"
    decls = _declarations(backend, code)
    built = build_theorem_record(
        _identity("wf"),
        code,
        decls[0],
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        created_at=_UTC,
    )
    assert isinstance(built, ExtractedDeclaration)
    assert built.theorem.declaration_full_name == "well_formed"
    assert built.theorem.statement_content_hash
    assert built.representation.view_status["raw_proof_stripped"].value == "ok"

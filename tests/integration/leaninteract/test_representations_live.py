"""LF-014 live: build repr_v1 views via #check against the fixture library."""

from __future__ import annotations

import datetime
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from leanfaith.config.paths import find_repo_root
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.representations.pipeline import TheoremForRepresentation, build_representations
from leanfaith.representations.views import signature_near_dup_hash
from leanfaith.schemas.ids import make_id

pytestmark = [
    pytest.mark.lean,
    pytest.mark.skipif(shutil.which("lake") is None, reason="Lean toolchain unavailable"),
]

_FIXTURES = find_repo_root(Path(__file__).parent) / "tests" / "lean_fixtures"
_CTX_FP = "0" * 64
_CTX = f"ctx:{_CTX_FP}"
_UTC = datetime.datetime(2026, 7, 11, tzinfo=datetime.UTC)


@pytest.fixture(scope="module")
def backend(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LeanInteractBackend]:
    instance = LeanInteractBackend(
        BackendSettings(
            project_dir=_FIXTURES,
            context_fingerprint=_CTX_FP,
            environment_schema_version=1,
            raw_response_dir=tmp_path_factory.mktemp("repr_raw"),
        )
    )
    yield instance
    instance.close()


def _theorem(name: str, proof_stripped: str) -> TheoremForRepresentation:
    return TheoremForRepresentation(
        theorem_id=make_id("thm", {"name": name}),
        full_name=name,
        proof_stripped=proof_stripped,
        context_id=_CTX,
    )


def test_build_repr_v1_views_from_fixture_library(backend: LeanInteractBackend) -> None:
    theorems = [
        _theorem("lf_add_comm", "theorem lf_add_comm (x y : Nat) : x + y = y + x := by sorry"),
        _theorem("lf_zero_add", "theorem lf_zero_add (n : Nat) : 0 + n = n := by sorry"),
        _theorem("lf_trivial", "theorem lf_trivial : True := by sorry"),
    ]
    records = build_representations(
        backend, theorems, imports="import LeanFaithFixtures", created_at=_UTC
    )
    assert len(records) == 3
    by_name = {r.theorem_id: r for r in records}
    for theorem in theorems:
        record = by_name[theorem.theorem_id]
        assert record.normalization_version == "repr_v1"
        assert record.view_status["raw_proof_stripped"].value == "ok"
        assert record.view_status["headless"].value == "ok"
        # signature_pp and signature_explicit come from the live elaborator.
        assert record.signature_pp, theorem.full_name
        assert record.view_status["signature_pp"].value == "ok"
        assert record.signature_explicit
        assert record.view_status["signature_explicit"].value == "ok"

    add_comm = by_name[theorems[0].theorem_id]
    assert add_comm.headless == "(x y : Nat) : x + y = y + x"
    assert "x + y = y + x" in (add_comm.signature_pp or "")
    # A near-dup signature hash is derivable and stable.
    assert len(signature_near_dup_hash(add_comm.signature_pp or "")) == 64


def test_unknown_declaration_marks_signature_failed(backend: LeanInteractBackend) -> None:
    theorems = [_theorem("does_not_exist_xyz", "theorem does_not_exist_xyz : True := by sorry")]
    records = build_representations(
        backend, theorems, imports="import LeanFaithFixtures", created_at=_UTC
    )
    record = records[0]
    # Required v0 views still succeed from the source; only the elaborated
    # views (which need the declaration to exist) fail explicitly.
    assert record.view_status["raw_proof_stripped"].value == "ok"
    assert record.view_status["headless"].value == "ok"
    assert record.view_status["signature_pp"].value == "failed"
    assert record.signature_pp is None


def test_semantic_atoms_and_operator_tree_live(backend: LeanInteractBackend) -> None:
    from leanfaith.representations.pipeline import build_representations

    theorems = [
        _theorem("lf_add_comm", "theorem lf_add_comm (x y : Nat) : x + y = y + x := by sorry"),
    ]
    records = build_representations(
        backend, theorems, imports="import LeanFaithFixtures", created_at=_UTC
    )
    record = records[0]
    assert record.view_status["semantic_atoms"].value == "ok", record.view_status
    assert record.view_status["operator_tree"].value == "ok"
    assert record.semantic_atoms is not None
    # The commutativity statement's atoms include the quantifiers, the addition
    # head, and equality (extracted from the elaborated Expr, not text).
    assert record.semantic_atoms.count("forall") == 2
    assert "const:HAdd.hAdd" in record.semantic_atoms
    assert "const:Eq" in record.semantic_atoms
    assert record.operator_tree is not None
    assert record.operator_tree["node_count"] > 5

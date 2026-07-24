"""LF-017 P02 live source/candidate re-elaboration and certificate audit."""

from __future__ import annotations

import datetime
import hashlib
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from leanfaith.config.paths import find_repo_root
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.protocol import LeanRequest, LeanStatus
from leanfaith.representations.pipeline import TheoremForRepresentation, build_representations
from leanfaith.schemas import (
    QualityTier,
    TheoremRecord,
    ValidationStatus,
    make_id,
)
from leanfaith.transforms.positives.p02_binders import P02BinderRule

pytestmark = [
    pytest.mark.lean,
    pytest.mark.skipif(shutil.which("lake") is None, reason="Lean toolchain unavailable"),
]

_FIXTURES = find_repo_root(Path(__file__).parent) / "tests" / "lean_fixtures"
_CTX_FP = "0" * 64
_CTX = f"ctx:{_CTX_FP}"
_UTC = datetime.datetime(2026, 7, 23, tzinfo=datetime.UTC)


@dataclass(frozen=True, slots=True)
class _LiveCase:
    name: str
    source_code: str
    expected_candidate_fragment: str


_CASES = (
    _LiveCase(
        name="p02_live_explicit_split",
        source_code=("theorem p02_live_explicit_split (x y : Nat) : x = x := by sorry"),
        expected_candidate_fragment="(x : Nat) (y : Nat)",
    ),
    _LiveCase(
        name="p02_live_explicit_merge",
        source_code=("theorem p02_live_explicit_merge (x : Nat) (y : Nat) : x = x := by sorry"),
        expected_candidate_fragment="(x y : Nat)",
    ),
    _LiveCase(
        name="p02_live_implicit_split",
        source_code=("theorem p02_live_implicit_split {α β : Type} (x : α) : x = x := by sorry"),
        expected_candidate_fragment="{α : Type} {β : Type}",
    ),
    _LiveCase(
        name="p02_live_strict_implicit_split",
        source_code=(
            "theorem p02_live_strict_implicit_split ⦃α β : Type⦄ (x : α) (y : β) : True := by sorry"
        ),
        expected_candidate_fragment="⦃α : Type⦄ ⦃β : Type⦄",
    ),
    _LiveCase(
        name="p02_live_outer_dependent_split",
        source_code=(
            "theorem p02_live_outer_dependent_split (n : Nat) (x y : Fin n) : x = x := by sorry"
        ),
        expected_candidate_fragment="(x : Fin n) (y : Fin n)",
    ),
)


@pytest.fixture(scope="module")
def backend(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LeanInteractBackend]:
    instance = LeanInteractBackend(
        BackendSettings(
            project_dir=_FIXTURES,
            context_fingerprint=_CTX_FP,
            environment_schema_version=1,
            raw_response_dir=tmp_path_factory.mktemp("p02_raw"),
        )
    )
    yield instance
    instance.close()


def _record(
    code: str,
    *,
    name: str,
    theorem_id: str,
    ancestry_id: str,
    parent_theorem_ids: tuple[str, ...] = (),
) -> TheoremRecord:
    root = make_id("anc", {"p02_live": name, "root": True})
    return TheoremRecord(
        theorem_id=theorem_id,
        ancestry_id=ancestry_id,
        root_ancestry_ids=(root,),
        parent_theorem_ids=parent_theorem_ids,
        source="p02_live_fixture",
        source_revision="p02_binders_v1",
        context_id=_CTX,
        declaration_kind="theorem",
        declaration_name=name,
        declaration_full_name=name,
        proof_stripped_declaration=code,
        inline_elaboration_source=code,
        is_proposition=True,
        elaboration_status=ValidationStatus.ELABORATES_WITH_PLACEHOLDER,
        statement_content_hash=hashlib.sha256(code.encode("utf-8")).hexdigest(),
    )


def _representation_input(
    theorem_id: str,
    code: str,
    name: str,
) -> TheoremForRepresentation:
    return TheoremForRepresentation(
        theorem_id=theorem_id,
        full_name=name,
        proof_stripped=code,
        context_id=_CTX,
        inline_declaration=True,
        inline_source=code,
    )


def _assert_reelaborates(
    backend: LeanInteractBackend,
    code: str,
    request_id: str,
    name: str,
) -> None:
    result = backend.run(
        LeanRequest(
            request_id=request_id,
            context_id=_CTX,
            code=code,
            declarations=True,
            allow_sorry=True,
        )
    )
    assert result.status == LeanStatus.VALID_WITH_SORRY, result.messages
    assert len(result.declarations) == 1
    assert result.declarations[0]["full_name"] == name


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.name)
def test_p02_source_and_candidate_reelaborate_and_certificate_passes(
    backend: LeanInteractBackend,
    case: _LiveCase,
) -> None:
    source_id = make_id("thm", {"p02_live": case.name, "side": "source"})
    source = _record(
        case.source_code,
        name=case.name,
        theorem_id=source_id,
        ancestry_id=make_id("anc", {"p02_live": case.name, "side": "source"}),
    )
    _assert_reelaborates(
        backend,
        case.source_code,
        f"p02-source-reelaborate-{case.name}",
        case.name,
    )
    source_representation = build_representations(
        backend,
        [_representation_input(source_id, case.source_code, case.name)],
        imports="",
        created_at=_UTC,
    )[0]
    assert source_representation.alpha_identity_fingerprint is not None

    # The rule binds every draft to the registry/config identity supplied by
    # the caller; the live fixture uses a deterministic test digest.
    rule = P02BinderRule(registry_hash="a" * 64)
    draft = rule.generate(source, source_representation, 17)[0]
    assert case.expected_candidate_fragment in draft.candidate_code
    _assert_reelaborates(
        backend,
        draft.candidate_code,
        f"p02-candidate-reelaborate-{case.name}",
        case.name,
    )

    candidate_id = make_id(
        "thm",
        {"p02_live": case.name, "draft_id": draft.draft_id},
    )
    candidate = _record(
        draft.candidate_code,
        name=case.name,
        theorem_id=candidate_id,
        ancestry_id=make_id(
            "anc",
            {"p02_live_candidate": case.name, "draft_id": draft.draft_id},
        ),
        parent_theorem_ids=(source_id,),
    )
    candidate_representation = build_representations(
        backend,
        [_representation_input(candidate_id, draft.candidate_code, case.name)],
        imports="",
        created_at=_UTC,
    )[0]

    audit = rule.audit(
        source,
        source_representation,
        candidate,
        candidate_representation,
        draft,
    )

    assert source_representation.alpha_identity_fingerprint == (
        candidate_representation.alpha_identity_fingerprint
    )
    assert audit.violation_codes == ()
    assert audit.recommended_quality_tier == QualityTier.PROVISIONAL
    assert audit.inverse_or_roundtrip_ok is True
    assert audit.metadata["binder_dependency_graph_equal"] is True
    assert audit.metadata["currying_applied"] is False
